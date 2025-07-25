# Authors: The MNE-Python contributors.
# License: BSD-3-Clause
# Copyright the MNE-Python contributors.

import datetime
import os
import pathlib
import pickle
import platform
import shutil
from contextlib import nullcontext
from copy import deepcopy
from functools import partial
from io import BytesIO
from pathlib import Path

import numpy as np
import pytest
from numpy.testing import assert_allclose, assert_array_almost_equal, assert_array_equal

from mne import (
    compute_proj_raw,
    concatenate_events,
    create_info,
    equalize_channels,
    events_from_annotations,
    find_events,
    make_fixed_length_epochs,
    pick_channels,
    pick_info,
    pick_types,
)
from mne._fiff.constants import FIFF
from mne._fiff.tag import _read_tag_header, read_tag
from mne.annotations import Annotations
from mne.datasets import testing
from mne.filter import filter_data
from mne.io import RawArray, base, concatenate_raws, match_channel_orders, read_raw_fif
from mne.io.tests.test_raw import _test_concat, _test_raw_reader
from mne.transforms import Transform
from mne.utils import (
    _dt_to_stamp,
    _record_warnings,
    assert_and_remove_boundary_annot,
    assert_object_equal,
    catch_logging,
    requires_mne,
    run_subprocess,
)

testing_path = testing.data_path(download=False)
data_dir = testing_path / "MEG" / "sample"
fif_fname = data_dir / "sample_audvis_trunc_raw.fif"
ms_fname = testing_path / "SSS" / "test_move_anon_raw.fif"
skip_fname = testing_path / "misc" / "intervalrecording_raw.fif"
tri_fname = testing_path / "SSS" / "TRIUX" / "triux_bmlhus_erm_raw.fif"

base_dir = Path(__file__).parents[2] / "tests" / "data"
test_fif_fname = base_dir / "test_raw.fif"
test_fif_gz_fname = base_dir / "test_raw.fif.gz"
ctf_fname = base_dir / "test_ctf_raw.fif"
ctf_comp_fname = base_dir / "test_ctf_comp_raw.fif"
fif_bad_marked_fname = base_dir / "test_withbads_raw.fif"
bad_file_works = base_dir / "test_bads.txt"
bad_file_wrong = base_dir / "test_wrong_bads.txt"
hp_fif_fname = base_dir / "test_chpi_raw_sss.fif"


@testing.requires_testing_data
def test_acq_skip(tmp_path):
    """Test treatment of acquisition skips."""
    raw = read_raw_fif(skip_fname, preload=True)
    picks = [1, 2, 10]
    assert len(raw.times) == 17000
    annotations = raw.annotations
    assert len(annotations) == 3  # there are 3 skips
    assert_allclose(annotations.onset, [14, 19, 23])
    assert_allclose(annotations.duration, [2.0, 2.0, 3.0])  # inclusive!
    data, times = raw.get_data(picks, reject_by_annotation="omit", return_times=True)
    expected_data, expected_times = zip(
        raw[picks, :2000],
        raw[picks, 4000:7000],
        raw[picks, 9000:11000],
        raw[picks, 14000:17000],
    )
    expected_times = np.concatenate(list(expected_times), axis=-1)
    assert_allclose(times, expected_times)
    expected_data = list(expected_data)
    assert_allclose(data, np.concatenate(expected_data, axis=-1), atol=1e-22)

    # Check that acquisition skips are handled properly in filtering
    kwargs = dict(l_freq=None, h_freq=50.0, fir_design="firwin")
    raw_filt = raw.copy().filter(picks=picks, **kwargs)
    for data in expected_data:
        filter_data(data, raw.info["sfreq"], copy=False, **kwargs)
    data = raw_filt.get_data(picks, reject_by_annotation="omit")
    assert_allclose(data, np.concatenate(expected_data, axis=-1), atol=1e-22)

    # Check that acquisition skips are handled properly during I/O
    fname = tmp_path / "test_raw.fif"
    raw.save(fname, fmt=raw.orig_format)
    # first: file size should not increase much (orig data is missing
    # 7 of 17 buffers, so if we write them out it should increase the file
    # size quite a bit.
    orig_size = skip_fname.lstat().st_size
    new_size = fname.lstat().st_size
    max_size = int(1.05 * orig_size)  # almost the same + annotations
    assert new_size < max_size, (new_size, max_size)
    raw_read = read_raw_fif(fname)
    assert raw_read.annotations is not None
    assert_allclose(raw.times, raw_read.times)
    assert_allclose(raw_read[:][0], raw[:][0], atol=1e-17)
    # Saving with a bad buffer length emits warning
    raw.pick(raw.ch_names[:2])
    with _record_warnings() as w:
        raw.save(fname, buffer_size_sec=0.5, overwrite=True)
    assert len(w) == 0
    with pytest.warns(RuntimeWarning, match="did not fit evenly"):
        raw.save(fname, buffer_size_sec=2.0, overwrite=True)


def test_fix_types():
    """Test fixing of channel types."""
    for fname, change, bads in (
        (hp_fif_fname, True, ["MEG0111"]),
        (test_fif_fname, False, []),
        (ctf_fname, False, []),
    ):
        raw = read_raw_fif(fname)
        raw.info["bads"] = bads
        mag_picks = pick_types(raw.info, meg="mag", exclude=[])
        other_picks = np.setdiff1d(np.arange(len(raw.ch_names)), mag_picks)
        # we don't actually have any files suffering from this problem, so
        # fake it
        if change:
            for ii in mag_picks:
                raw.info["chs"][ii]["coil_type"] = FIFF.FIFFV_COIL_VV_MAG_T2
        orig_types = np.array([ch["coil_type"] for ch in raw.info["chs"]])
        raw.fix_mag_coil_types()
        new_types = np.array([ch["coil_type"] for ch in raw.info["chs"]])
        if not change:
            assert_array_equal(orig_types, new_types)
        else:
            assert_array_equal(orig_types[other_picks], new_types[other_picks])
            assert (orig_types[mag_picks] != new_types[mag_picks]).all()
            assert (new_types[mag_picks] == FIFF.FIFFV_COIL_VV_MAG_T3).all()


def test_concat(tmp_path):
    """Test RawFIF concatenation."""
    # we trim the file to save lots of memory and some time
    raw = read_raw_fif(test_fif_fname)
    raw.crop(0, 2.0)
    test_name = tmp_path / "test_raw.fif"
    raw.save(test_name)
    # now run the standard test
    _test_concat(partial(read_raw_fif), test_name)


@testing.requires_testing_data
def test_hash_raw():
    """Test hashing raw objects."""
    raw = read_raw_fif(fif_fname)
    pytest.raises(RuntimeError, raw.__hash__)
    raw = read_raw_fif(fif_fname).crop(0, 0.5)
    raw_size = raw._size
    raw.load_data()
    raw_load_size = raw._size
    assert raw_size < raw_load_size
    raw_2 = read_raw_fif(fif_fname).crop(0, 0.5)
    raw_2.load_data()
    assert hash(raw) == hash(raw_2)
    # do NOT use assert_equal here, failing output is terrible
    assert pickle.dumps(raw) == pickle.dumps(raw_2)

    raw_2._data[0, 0] -= 1
    assert hash(raw) != hash(raw_2)


@testing.requires_testing_data
def test_maxshield():
    """Test maxshield warning."""
    with pytest.warns(RuntimeWarning, match="Internal Active Shielding") as w:
        read_raw_fif(ms_fname, allow_maxshield=True)
    assert "test_raw_fiff.py" in w[0].filename


@testing.requires_testing_data
def test_subject_info(tmp_path):
    """Test reading subject information."""
    raw = read_raw_fif(fif_fname).crop(0, 1)
    assert raw.info["subject_info"] is None
    # fake some subject data
    keys = ["id", "his_id", "last_name", "first_name", "birthday", "sex", "hand"]
    vals = [1, "foobar", "bar", "foo", datetime.date(1901, 2, 3), 0, 1]
    subject_info = dict()
    for key, val in zip(keys, vals):
        subject_info[key] = val
    raw.info["subject_info"] = subject_info
    out_fname = tmp_path / "test_subj_info_raw.fif"
    raw.save(out_fname, overwrite=True)
    raw_read = read_raw_fif(out_fname)
    for key in keys:
        assert subject_info[key] == raw_read.info["subject_info"][key]
    assert raw.info["meas_date"] == raw_read.info["meas_date"]

    for key in ["secs", "usecs", "version"]:
        assert raw.info["meas_id"][key] == raw_read.info["meas_id"][key]
    assert_array_equal(
        raw.info["meas_id"]["machid"], raw_read.info["meas_id"]["machid"]
    )


@testing.requires_testing_data
def test_copy_append():
    """Test raw copying and appending combinations."""
    raw = read_raw_fif(fif_fname, preload=True).copy()
    raw_full = read_raw_fif(fif_fname)
    raw_full.append(raw)
    data = raw_full[:, :][0]
    assert data.shape[1] == 2 * raw._data.shape[1]


@testing.requires_testing_data
def test_output_formats(tmp_path):
    """Test saving and loading raw data using multiple formats."""
    formats = ["short", "int", "single", "double"]
    tols = [1e-4, 1e-7, 1e-7, 1e-15]

    # let's fake a raw file with different formats
    raw = read_raw_fif(test_fif_fname).crop(0, 1)

    temp_file = tmp_path / "raw.fif"
    for ii, (fmt, tol) in enumerate(zip(formats, tols)):
        # Let's test the overwriting error throwing while we're at it
        if ii > 0:
            pytest.raises(OSError, raw.save, temp_file, fmt=fmt)
        raw.save(temp_file, fmt=fmt, overwrite=True)
        raw2 = read_raw_fif(temp_file)
        raw2_data = raw2[:, :][0]
        assert_allclose(raw2_data, raw[:, :][0], rtol=tol, atol=1e-25)
        assert raw2.orig_format == fmt


def _compare_combo(raw, new, times, n_times):
    """Compare data."""
    for ti in times:  # let's do a subset of points for speed
        orig = raw[:, ti % n_times][0]
        # these are almost_equals because of possible dtype differences
        assert_allclose(orig, new[:, ti][0])


@pytest.mark.slowtest
@testing.requires_testing_data
def test_multiple_files(tmp_path):
    """Test loading multiple files simultaneously."""
    # split file
    raw = read_raw_fif(fif_fname).crop(0, 10)
    raw.load_data()
    raw.load_data()  # test no operation
    split_size = 3.0  # in seconds
    sfreq = raw.info["sfreq"]
    nsamp = raw.last_samp - raw.first_samp
    tmins = np.round(np.arange(0.0, nsamp, split_size * sfreq))
    tmaxs = np.concatenate((tmins[1:] - 1, [nsamp]))
    tmaxs /= sfreq
    tmins /= sfreq
    assert raw.n_times == len(raw.times)

    # going in reverse order so the last fname is the first file (need later)
    raws = [None] * len(tmins)
    for ri in range(len(tmins) - 1, -1, -1):
        fname = tmp_path / (f"test_raw_split-{ri}_raw.fif")
        raw.save(fname, tmin=tmins[ri], tmax=tmaxs[ri])
        raws[ri] = read_raw_fif(fname)
        assert (
            len(raws[ri].times)
            == int(round((tmaxs[ri] - tmins[ri]) * raw.info["sfreq"])) + 1
        )  # + 1 b/c inclusive
    events = [find_events(r, stim_channel="STI 014") for r in raws]
    last_samps = [r.last_samp for r in raws]
    first_samps = [r.first_samp for r in raws]

    # test concatenation of split file
    pytest.raises(ValueError, concatenate_raws, raws, True, events[1:])
    all_raw_1, events1 = concatenate_raws(raws, preload=False, events_list=events)
    assert_allclose(all_raw_1.times, raw.times)
    assert raw.first_samp == all_raw_1.first_samp
    assert raw.last_samp == all_raw_1.last_samp
    assert_allclose(raw[:, :][0], all_raw_1[:, :][0])
    raws[0] = read_raw_fif(fname)
    all_raw_2 = concatenate_raws(raws, preload=True)
    assert_allclose(raw[:, :][0], all_raw_2[:, :][0])

    # test proper event treatment for split files
    events2 = concatenate_events(events, first_samps, last_samps)
    events3 = find_events(all_raw_2, stim_channel="STI 014")
    assert_array_equal(events1, events2)
    assert_array_equal(events1, events3)

    # test various methods of combining files
    raw = read_raw_fif(fif_fname, preload=True)
    n_times = raw.n_times
    # make sure that all our data match
    times = list(range(0, 2 * n_times, 999))
    # add potentially problematic points
    times.extend([n_times - 1, n_times, 2 * n_times - 1])

    raw_combo0 = concatenate_raws(
        [read_raw_fif(f) for f in [fif_fname, fif_fname]], preload=True
    )
    _compare_combo(raw, raw_combo0, times, n_times)
    raw_combo = concatenate_raws(
        [read_raw_fif(f) for f in [fif_fname, fif_fname]], preload=False
    )
    _compare_combo(raw, raw_combo, times, n_times)
    raw_combo = concatenate_raws(
        [read_raw_fif(f) for f in [fif_fname, fif_fname]], preload="memmap8.dat"
    )
    _compare_combo(raw, raw_combo, times, n_times)
    assert raw[:, :][0].shape[1] * 2 == raw_combo0[:, :][0].shape[1]
    assert raw_combo0[:, :][0].shape[1] == raw_combo0.n_times

    # with all data preloaded, result should be preloaded
    raw_combo = read_raw_fif(fif_fname, preload=True)
    raw_combo.append(read_raw_fif(fif_fname, preload=True))
    assert raw_combo.preload is True
    assert raw_combo.n_times == raw_combo._data.shape[1]
    _compare_combo(raw, raw_combo, times, n_times)

    # with any data not preloaded, don't set result as preloaded
    raw_combo = concatenate_raws(
        [read_raw_fif(fif_fname, preload=True), read_raw_fif(fif_fname, preload=False)]
    )
    assert raw_combo.preload is False
    assert_array_equal(
        find_events(raw_combo, stim_channel="STI 014"),
        find_events(raw_combo0, stim_channel="STI 014"),
    )
    _compare_combo(raw, raw_combo, times, n_times)

    # user should be able to force data to be preloaded upon concat
    raw_combo = concatenate_raws(
        [read_raw_fif(fif_fname, preload=False), read_raw_fif(fif_fname, preload=True)],
        preload=True,
    )
    assert raw_combo.preload is True
    _compare_combo(raw, raw_combo, times, n_times)

    raw_combo = concatenate_raws(
        [read_raw_fif(fif_fname, preload=False), read_raw_fif(fif_fname, preload=True)],
        preload="memmap3.dat",
    )
    _compare_combo(raw, raw_combo, times, n_times)

    raw_combo = concatenate_raws(
        [read_raw_fif(fif_fname, preload=True), read_raw_fif(fif_fname, preload=True)],
        preload="memmap4.dat",
    )
    _compare_combo(raw, raw_combo, times, n_times)

    raw_combo = concatenate_raws(
        [
            read_raw_fif(fif_fname, preload=False),
            read_raw_fif(fif_fname, preload=False),
        ],
        preload="memmap5.dat",
    )
    _compare_combo(raw, raw_combo, times, n_times)

    # verify that combining raws with different projectors throws an exception
    raw.add_proj([], remove_existing=True)
    pytest.raises(ValueError, raw.append, read_raw_fif(fif_fname, preload=True))

    # now test event treatment for concatenated raw files
    events = [
        find_events(raw, stim_channel="STI 014"),
        find_events(raw, stim_channel="STI 014"),
    ]
    last_samps = [raw.last_samp, raw.last_samp]
    first_samps = [raw.first_samp, raw.first_samp]
    events = concatenate_events(events, first_samps, last_samps)
    events2 = find_events(raw_combo0, stim_channel="STI 014")
    assert_array_equal(events, events2)

    # check out the len method
    assert len(raw) == raw.n_times
    assert len(raw) == raw.last_samp - raw.first_samp + 1


@testing.requires_testing_data
@pytest.mark.parametrize("on_mismatch", ("ignore", "warn", "raise"))
def test_concatenate_raws(on_mismatch):
    """Test error handling during raw concatenation."""
    raw = read_raw_fif(fif_fname).crop(0, 10)
    raws = [raw, raw.copy()]
    raws[1].info["dev_head_t"]["trans"] += 0.1
    kws = dict(raws=raws, on_mismatch=on_mismatch)

    if on_mismatch == "ignore":
        concatenate_raws(**kws)
    elif on_mismatch == "warn":
        with pytest.warns(RuntimeWarning, match="different head positions"):
            concatenate_raws(**kws)
    elif on_mismatch == "raise":
        with pytest.raises(ValueError, match="different head positions"):
            concatenate_raws(**kws)


def _create_toy_data(n_channels=3, sfreq=250, seed=None):
    rng = np.random.default_rng(seed)
    data = rng.standard_normal(size=(n_channels, 50 * sfreq)) * 5e-6
    info = create_info(n_channels, sfreq, "eeg")
    return RawArray(data, info)


def test_concatenate_raws_bads_order():
    """Test concatenation of raws when the order of *bad* channels varies."""
    raw0 = _create_toy_data()
    raw1 = _create_toy_data()

    # Test bad channel order
    raw0.info["bads"] = ["0", "1"]
    raw1.info["bads"] = ["1", "0"]

    # raw0 is modified in-place and therefore copied
    raw_concat = concatenate_raws([raw0.copy(), raw1])

    # Check data are equal
    data_concat = np.concatenate([raw0.get_data(), raw1.get_data()], 1)
    assert np.all(raw_concat.get_data() == data_concat)

    # Check bad channels
    assert set(raw_concat.info["bads"]) == {"0", "1"}

    # Bad channel mismatch raises
    raw2 = raw1.copy()
    raw2.info["bads"] = ["0", "2"]
    with pytest.raises(ValueError, match="bads.*must match"):
        concatenate_raws([raw0, raw2])

    # Type mismatch raises
    epochs1 = make_fixed_length_epochs(raw1)
    with pytest.raises(ValueError, match="type.*must match"):
        concatenate_raws([raw0, epochs1.load_data()])

    # Sample rate mismatch
    raw3 = _create_toy_data(sfreq=500)
    raw3.info["bads"] = ["0", "1"]
    with pytest.raises(ValueError, match="info.*must match"):
        concatenate_raws([raw0, raw3])

    # Number of channels mismatch
    raw4 = _create_toy_data(n_channels=4)
    with pytest.raises(ValueError, match="nchan.*must match"):
        concatenate_raws([raw0, raw4])


def test_concatenate_raws_order():
    """Test concatenation of raws when the order of *good* channels varies."""
    raw0 = _create_toy_data(n_channels=2)
    raw0._data[0] = np.zeros_like(raw0._data[0])  # set one channel zero

    # Create copy and concatenate raws
    raw1 = raw0.copy()
    raw_concat = concatenate_raws([raw0.copy(), raw1])
    assert raw0.ch_names == raw1.ch_names == raw_concat.ch_names == ["0", "1"]
    ch0 = raw_concat.get_data(picks=["0"])
    assert np.all(ch0 == 0)

    # Change the order of the channels and concatenate again
    raw1.reorder_channels(["1", "0"])
    assert raw1.ch_names == ["1", "0"]
    raws = [raw0.copy(), raw1]
    with pytest.raises(ValueError, match="Channel order must match."):
        # Fails now due to wrong order of channels
        raw_concat = concatenate_raws(raws)

    with pytest.raises(ValueError, match="Channel order must match."):
        # still fails, because raws is copied and not changed in place
        match_channel_orders(insts=raws, copy=True)
        raw_concat = concatenate_raws(raws)

    # Now passes because all raws have the same order
    match_channel_orders(insts=raws, copy=False)
    raw_concat = concatenate_raws(raws)
    ch0 = raw_concat.get_data(picks=["0"])
    assert np.all(ch0 == 0)


@testing.requires_testing_data
@pytest.mark.parametrize(
    "mod",
    (
        "meg",
        pytest.param(
            "raw",
            marks=[
                pytest.mark.filterwarnings(
                    "ignore:.*naming conventions.*:RuntimeWarning"
                ),
                pytest.mark.slowtest,
            ],
        ),
    ),
)
def test_split_files(tmp_path, mod, monkeypatch):
    """Test writing and reading of split raw files."""
    raw_1 = read_raw_fif(fif_fname, preload=True)
    # Test a very close corner case

    assert_allclose(raw_1.buffer_size_sec, 10.0, atol=1e-2)  # samp rate
    split_fname = tmp_path / f"split_raw_{mod}.fif"
    # intended filenames
    split_fname_elekta_part2 = tmp_path / f"split_raw_{mod}-1.fif"
    split_fname_bids_part1 = tmp_path / f"split_raw_split-01_{mod}.fif"
    split_fname_bids_part2 = tmp_path / f"split_raw_split-02_{mod}.fif"
    raw_1.set_annotations(Annotations([2.0], [5.5], "test"))

    # Check that if BIDS is used and no split is needed it defaults to
    # simple writing without _split- entity.
    split_fnames = raw_1.save(split_fname, split_naming="bids", verbose=True)
    assert split_fname.is_file()
    assert not split_fname_bids_part1.is_file()
    assert split_fnames == [split_fname]

    for split_naming in ("neuromag", "bids"):
        with pytest.raises(FileExistsError, match="Destination file"):
            raw_1.save(split_fname, split_naming=split_naming, verbose=True)
    os.remove(split_fname)
    with open(split_fname_bids_part1, "w"):
        pass
    with pytest.raises(FileExistsError, match="Destination file"):
        raw_1.save(split_fname, split_naming="bids", verbose=True)
    assert not split_fname.is_file()
    split_fnames = raw_1.save(
        split_fname, split_naming="neuromag", verbose=True
    )  # okay
    os.remove(split_fname)
    os.remove(split_fname_bids_part1)
    # Multiple splits
    split_filenames = raw_1.save(
        split_fname, buffer_size_sec=1.0, split_size="10MB", verbose=True
    )
    # check that the filenames match the intended pattern
    assert split_fname.is_file()
    assert split_fname_elekta_part2.is_file()
    assert split_filenames == [split_fname, split_fname_elekta_part2]
    # check that filenames are being formatted correctly for BIDS
    split_filenames = raw_1.save(
        split_fname,
        buffer_size_sec=1.0,
        split_size="10MB",
        split_naming="bids",
        overwrite=True,
        verbose=True,
    )
    assert split_fname_bids_part1.is_file()
    assert split_fname_bids_part2.is_file()
    assert split_filenames == [split_fname_bids_part1, split_fname_bids_part2]

    annot = Annotations(np.arange(20), np.ones((20,)), "test")
    raw_1.set_annotations(annot)
    split_fname = tmp_path / f"split_{mod}.fif"
    raw_1.save(split_fname, buffer_size_sec=1.0, split_size="10MB")
    raw_2 = read_raw_fif(split_fname)
    assert_allclose(raw_2.buffer_size_sec, 1.0, atol=1e-2)  # samp rate
    assert_allclose(raw_1.annotations.onset, raw_2.annotations.onset)
    assert_allclose(
        raw_1.annotations.duration,
        raw_2.annotations.duration,
        rtol=0.001 / raw_2.info["sfreq"],
    )
    assert_array_equal(raw_1.annotations.description, raw_2.annotations.description)

    data_1, times_1 = raw_1[:, :]
    data_2, times_2 = raw_2[:, :]
    assert_array_equal(data_1, data_2)
    assert_array_equal(times_1, times_2)

    raw_bids = read_raw_fif(split_fname_bids_part1)
    data_bids, times_bids = raw_bids[:, :]
    assert_array_equal(data_1, data_bids)
    assert_array_equal(times_1, times_bids)
    del raw_bids
    # split missing behaviors
    os.remove(split_fname_bids_part2)
    with pytest.raises(ValueError, match="manually renamed"):
        read_raw_fif(split_fname_bids_part1, on_split_missing="raise")
    with pytest.warns(RuntimeWarning, match="Split raw file detected"):
        read_raw_fif(split_fname_bids_part1, on_split_missing="warn")
    read_raw_fif(split_fname_bids_part1, on_split_missing="ignore")

    # test the case where we only end up with one buffer to write
    # (GH#3210). These tests rely on writing meas info and annotations
    # taking up a certain number of bytes, so if we change those functions
    # somehow, the numbers below for e.g. split_size might need to be
    # adjusted.
    raw_crop = raw_1.copy().crop(0, 5)
    raw_crop.set_annotations(Annotations([2.0], [5.5], "test"), emit_warning=False)
    with pytest.raises(ValueError, match="after writing measurement information"):
        raw_crop.save(
            split_fname,
            split_size="1MB",  # too small a size
            buffer_size_sec=1.0,
            overwrite=True,
        )
    with pytest.raises(ValueError, match="too large for the given split size"):
        raw_crop.save(
            split_fname,
            split_size=3003000,  # still too small, now after Info
            buffer_size_sec=1.0,
            overwrite=True,
        )
    # just barely big enough here; the right size to write exactly one buffer
    # at a time so we hit GH#3210 if we aren't careful
    raw_crop.save(split_fname, split_size="4.5MB", buffer_size_sec=1.0, overwrite=True)
    raw_read = read_raw_fif(split_fname)
    assert_allclose(raw_crop[:][0], raw_read[:][0], atol=1e-20)

    # Check our buffer arithmetic

    # 1 buffer required
    raw_crop = raw_1.copy().crop(0, 1)
    raw_crop.save(split_fname, buffer_size_sec=1.0, overwrite=True)
    raw_read = read_raw_fif(split_fname)
    assert_array_equal(np.diff(raw_read._raw_extras[0]["bounds"]), (301,))
    assert_allclose(raw_crop[:][0], raw_read[:][0])
    # 2 buffers required
    raw_crop.save(split_fname, buffer_size_sec=0.5, overwrite=True)
    raw_read = read_raw_fif(split_fname)
    assert_array_equal(np.diff(raw_read._raw_extras[0]["bounds"]), (151, 150))
    assert_allclose(raw_crop[:][0], raw_read[:][0])
    # 2 buffers required
    raw_crop.save(
        split_fname, buffer_size_sec=1.0 - 1.01 / raw_crop.info["sfreq"], overwrite=True
    )
    raw_read = read_raw_fif(split_fname)
    assert_array_equal(np.diff(raw_read._raw_extras[0]["bounds"]), (300, 1))
    assert_allclose(raw_crop[:][0], raw_read[:][0])
    raw_crop.save(
        split_fname, buffer_size_sec=1.0 - 2.01 / raw_crop.info["sfreq"], overwrite=True
    )
    raw_read = read_raw_fif(split_fname)
    assert_array_equal(np.diff(raw_read._raw_extras[0]["bounds"]), (299, 2))
    assert_allclose(raw_crop[:][0], raw_read[:][0])

    # proper ending
    assert tmp_path.is_dir()
    with pytest.raises(ValueError, match="must end with an underscore"):
        raw_crop.save(tmp_path / "test.fif", split_naming="bids", verbose="error")

    # reserved file is deleted
    fname = tmp_path / f"test_{mod}.fif"
    with monkeypatch.context() as m:
        m.setattr(base, "_write_raw_data", _err)
        with pytest.raises(RuntimeError, match="Killed mid-write"):
            raw_1.save(fname, split_size="10MB", split_naming="bids")
    assert fname.is_file()
    assert not (tmp_path / "test_split-01_{mod}.fif").is_file()

    # MAX_N_SPLITS exceeded
    raw = RawArray(np.zeros((1, 2000000)), create_info(1, 1000.0, "eeg"))
    fname.unlink()
    kwargs = dict(split_size="2MB", overwrite=True, verbose=True)
    with monkeypatch.context() as m:
        m.setattr(base, "MAX_N_SPLITS", 2)
        with pytest.raises(RuntimeError, match="Exceeded maximum number of splits"):
            raw.save(fname, split_naming="bids", **kwargs)
    fname_1, fname_2, fname_3 = (
        (tmp_path / f"test_split-{ii:02d}_{mod}.fif") for ii in range(1, 4)
    )
    assert not fname.is_file()
    assert fname_1.is_file()
    assert fname_2.is_file()
    assert not fname_3.is_file()
    with monkeypatch.context() as m:
        m.setattr(base, "MAX_N_SPLITS", 2)
        with pytest.raises(RuntimeError, match="Exceeded maximum number of splits"):
            raw.save(fname, split_naming="neuromag", **kwargs)
    fname_2, fname_3 = ((tmp_path / f"test_{mod}-{ii}.fif") for ii in range(1, 3))
    assert fname.is_file()
    assert fname_2.is_file()
    assert not fname_3.is_file()


def test_bids_split_files(tmp_path):
    """Test that BIDS split files are written safely."""
    mne_bids = pytest.importorskip("mne_bids")
    bids_path = mne_bids.BIDSPath(
        root=tmp_path,
        subject="01",
        datatype="meg",
        split="01",
        suffix="raw",
        extension=".fif",
        check=False,
    )
    (tmp_path / "sub-01" / "meg").mkdir(parents=True)
    raw = read_raw_fif(test_fif_fname)
    save_kwargs = dict(
        buffer_size_sec=1.0, split_size="10MB", split_naming="bids", verbose=True
    )
    with pytest.raises(ValueError, match="Passing a BIDSPath"):
        raw.save(bids_path, **save_kwargs)
    bids_path.split = None
    want_paths = [
        Path(bids_path.copy().update(split=f"{ii:02d}").fpath) for ii in range(1, 3)
    ]
    for want_path in want_paths:
        assert not want_path.is_file()
    raw.save(bids_path, **save_kwargs)
    for want_path in want_paths:
        assert want_path.is_file(), want_path


def _err(*args, **kwargs):
    raise RuntimeError("Killed mid-write")


def _no_write_file_name(fid, kind, data):
    assert kind == FIFF.FIFF_REF_FILE_NAME  # the only string we actually write
    return


def test_split_numbers(tmp_path, monkeypatch):
    """Test handling of split files using numbers instead of names."""
    monkeypatch.setattr(base, "write_string", _no_write_file_name)
    raw = read_raw_fif(test_fif_fname).pick("eeg")
    # gh-8339
    dashes_fname = tmp_path / "sub-1_ses-2_task-3_raw.fif"
    raw.save(dashes_fname, split_size="5MB", buffer_size_sec=1.0)
    assert dashes_fname.is_file()
    next_fname = Path(str(dashes_fname)[:-4] + "-1.fif")
    assert next_fname.is_file()
    raw_read = read_raw_fif(dashes_fname)
    assert_allclose(raw.times, raw_read.times)
    assert_allclose(raw.get_data(), raw_read.get_data(), atol=1e-16)


def test_load_bad_channels(tmp_path):
    """Test reading/writing of bad channels."""
    # Load correctly marked file (manually done in mne_process_raw)
    raw_marked = read_raw_fif(fif_bad_marked_fname)
    correct_bads = raw_marked.info["bads"]
    raw = read_raw_fif(test_fif_fname)
    # Make sure it starts clean
    assert_array_equal(raw.info["bads"], [])

    # Test normal case
    raw.load_bad_channels(bad_file_works)
    # Write it out, read it in, and check
    raw.save(tmp_path / "foo_raw.fif")
    raw_new = read_raw_fif(tmp_path / "foo_raw.fif")
    assert correct_bads == raw_new.info["bads"]
    # Reset it
    raw.info["bads"] = []

    # Test bad case
    pytest.raises(ValueError, raw.load_bad_channels, bad_file_wrong)

    # Test forcing the bad case
    with pytest.warns(RuntimeWarning, match="1 bad channel"):
        raw.load_bad_channels(bad_file_wrong, force=True)

    # write it out, read it in, and check
    raw.save(tmp_path / "foo_raw.fif", overwrite=True)
    raw_new = read_raw_fif(tmp_path / "foo_raw.fif")
    assert correct_bads == raw_new.info["bads"]

    # Check that bad channels are cleared
    raw.load_bad_channels(None)
    raw.save(tmp_path / "foo_raw.fif", overwrite=True)
    raw_new = read_raw_fif(tmp_path / "foo_raw.fif")
    assert raw_new.info["bads"] == []


@pytest.mark.slowtest
@testing.requires_testing_data
def test_io_raw(tmp_path):
    """Test IO for raw data (Neuromag)."""
    rng = np.random.RandomState(0)
    # test unicode io
    for chars in ["äöé", "a"]:
        with read_raw_fif(fif_fname) as r:
            assert "Raw" in repr(r)
            assert fif_fname.name in repr(r)
            r.info["description"] = chars
            temp_file = tmp_path / "raw.fif"
            r.save(temp_file, overwrite=True)
            with read_raw_fif(temp_file) as r2:
                desc2 = r2.info["description"]
            assert desc2 == chars

    # Let's construct a simple test for IO first
    raw = read_raw_fif(fif_fname).crop(0, 3.5)
    raw.load_data()
    # put in some data that we know the values of
    data = rng.randn(raw._data.shape[0], raw._data.shape[1])
    raw._data[:, :] = data
    # save it somewhere
    fname = tmp_path / "test_copy_raw.fif"
    raw.save(fname, buffer_size_sec=1.0)
    # read it in, make sure the whole thing matches
    raw = read_raw_fif(fname)
    assert_allclose(data, raw[:, :][0], rtol=1e-6, atol=1e-20)
    # let's read portions across the 1-s tag boundary, too
    inds = raw.time_as_index([1.75, 2.25])
    sl = slice(inds[0], inds[1])
    assert_allclose(data[:, sl], raw[:, sl][0], rtol=1e-6, atol=1e-20)

    # missing dir raises informative error
    with pytest.raises(FileNotFoundError, match="parent directory does not exist"):
        raw.save(tmp_path / "foo" / "test_raw.fif", split_size="1MB")


@pytest.mark.parametrize(
    "fname_in, fname_out",
    [
        (test_fif_fname, "raw.fif"),
        pytest.param(test_fif_gz_fname, "raw.fif.gz", marks=pytest.mark.slowtest),
        (ctf_fname, "raw.fif"),
    ],
)
def test_io_raw_additional(fname_in, fname_out, tmp_path):
    """Test IO for raw data (Neuromag + CTF + gz)."""
    fname_out = tmp_path / fname_out
    raw = read_raw_fif(fname_in).crop(0, 2)

    nchan = raw.info["nchan"]
    ch_names = raw.info["ch_names"]
    meg_channels_idx = [k for k in range(nchan) if ch_names[k][0] == "M"]
    n_channels = 100
    meg_channels_idx = meg_channels_idx[:n_channels]
    start, stop = raw.time_as_index([0, 5], use_rounding=True)
    data, times = raw[meg_channels_idx, start : (stop + 1)]
    meg_ch_names = [ch_names[k] for k in meg_channels_idx]

    # Set up pick list: MEG + STI 014 - bad channels
    include = ["STI 014"]
    include += meg_ch_names
    picks = pick_types(
        raw.info,
        meg=True,
        eeg=False,
        stim=True,
        misc=True,
        ref_meg=True,
        include=include,
        exclude="bads",
    )

    # Writing with drop_small_buffer True
    raw.save(
        fname_out,
        picks,
        tmin=0,
        tmax=4,
        buffer_size_sec=3,
        drop_small_buffer=True,
        overwrite=True,
    )
    raw2 = read_raw_fif(fname_out)

    sel = pick_channels(raw2.ch_names, meg_ch_names)
    data2, times2 = raw2[sel, :]
    assert times2.max() <= 3

    # Writing
    raw.save(fname_out, picks, tmin=0, tmax=5, overwrite=True)

    if fname_in in (fif_fname, fif_fname.with_suffix(fif_fname.suffix + ".gz")):
        assert len(raw.info["dig"]) == 146

    raw2 = read_raw_fif(fname_out)

    sel = pick_channels(raw2.ch_names, meg_ch_names)
    data2, times2 = raw2[sel, :]

    assert_allclose(data, data2, rtol=1e-6, atol=1e-20)
    assert_allclose(times, times2)
    assert_allclose(raw.info["sfreq"], raw2.info["sfreq"], rtol=1e-5)

    # check transformations
    for trans in ["dev_head_t", "dev_ctf_t", "ctf_head_t"]:
        if raw.info[trans] is None:
            assert raw2.info[trans] is None
        else:
            assert_array_equal(raw.info[trans]["trans"], raw2.info[trans]["trans"])

            # check transformation 'from' and 'to'
            if trans.startswith("dev"):
                from_id = FIFF.FIFFV_COORD_DEVICE
            else:
                from_id = FIFF.FIFFV_MNE_COORD_CTF_HEAD
            if trans[4:8] == "head":
                to_id = FIFF.FIFFV_COORD_HEAD
            else:
                to_id = FIFF.FIFFV_MNE_COORD_CTF_HEAD
            for raw_ in [raw, raw2]:
                assert raw_.info[trans]["from"] == from_id
                assert raw_.info[trans]["to"] == to_id

    if fname_in in (fif_fname, fif_fname.with_suffix(fif_fname.suffix + ".gz")):
        assert_allclose(raw.info["dig"][0]["r"], raw2.info["dig"][0]["r"])

    # test warnings on bad filenames
    raw_badname = tmp_path / "test-bad-name.fif.gz"
    with pytest.warns(RuntimeWarning, match="raw.fif"):
        raw.save(raw_badname)
    with pytest.warns(RuntimeWarning, match="raw.fif"):
        read_raw_fif(raw_badname)


@testing.requires_testing_data
@pytest.mark.parametrize("dtype", ("complex128", "complex64"))
def test_io_complex(tmp_path, dtype):
    """Test IO with complex data types."""
    rng = np.random.RandomState(0)
    n_ch = 5
    raw = read_raw_fif(fif_fname).crop(0, 1).pick(np.arange(n_ch)).load_data()
    data_orig = raw.get_data()
    imag_rand = np.array(1j * rng.randn(n_ch, len(raw.times)), dtype=dtype)
    raw_cp = raw.copy()
    raw_cp._data = np.array(raw_cp._data, dtype)
    raw_cp._data += imag_rand
    with pytest.warns(RuntimeWarning, match="Saving .* complex data."):
        raw_cp.save(tmp_path / "raw.fif", overwrite=True)

    raw2 = read_raw_fif(tmp_path / "raw.fif")
    raw2_data, _ = raw2[:]
    assert_allclose(raw2_data, raw_cp._data)
    # with preloading
    raw2 = read_raw_fif(tmp_path / "raw.fif", preload=True)
    raw2_data, _ = raw2[:]
    assert_allclose(raw2_data, raw_cp._data)
    assert_allclose(data_orig, raw_cp._data.real)


@testing.requires_testing_data
def test_getitem():
    """Test getitem/indexing of Raw."""
    for preload in [False, True, "memmap1.dat"]:
        raw = read_raw_fif(fif_fname, preload=preload)
        data, times = raw[0, :]
        data1, times1 = raw[0]
        assert_array_equal(data, data1)
        assert_array_equal(times, times1)
        data, times = raw[0:2, :]
        data1, times1 = raw[0:2]
        assert_array_equal(data, data1)
        assert_array_equal(times, times1)
        data1, times1 = raw[[0, 1]]
        assert_array_equal(data, data1)
        assert_array_equal(times, times1)
        assert_array_equal(raw[raw.ch_names[0]][0][0], raw[0][0][0])
        assert_array_equal(
            raw[-10:-1, :][0], raw[len(raw.ch_names) - 10 : len(raw.ch_names) - 1, :][0]
        )
        with pytest.raises(ValueError, match="No appropriate channels"):
            raw[slice(-len(raw.ch_names) - 1), slice(None)]
        with pytest.raises(IndexError, match="must be"):
            raw[-1000]


@testing.requires_testing_data
def test_iter():
    """Test iterating over Raw via __getitem__()."""
    raw = read_raw_fif(fif_fname).pick("eeg")  # 60 EEG channels
    for i, _ in enumerate(raw):  # iterate over channels
        pass
    assert i == 59  # 60 channels means iterating from 0 to 59


@testing.requires_testing_data
def test_proj(tmp_path):
    """Test SSP proj operations."""
    for proj in [True, False]:
        raw = read_raw_fif(fif_fname, preload=False)
        if proj:
            raw.apply_proj()
        assert all(p["active"] == proj for p in raw.info["projs"])

        data, times = raw[0:2, :]
        data1, times1 = raw[0:2]
        assert_array_equal(data, data1)
        assert_array_equal(times, times1)

        # test adding / deleting proj
        if proj:
            pytest.raises(ValueError, raw.add_proj, [], {"remove_existing": True})
            pytest.raises(ValueError, raw.del_proj, 0)
        else:
            projs = deepcopy(raw.info["projs"])
            n_proj = len(raw.info["projs"])
            raw.del_proj(0)
            assert len(raw.info["projs"]) == n_proj - 1
            raw.add_proj(projs, remove_existing=False)
            # Test that already existing projections are not added.
            assert len(raw.info["projs"]) == n_proj
            raw.add_proj(projs[:-1], remove_existing=True)
            assert len(raw.info["projs"]) == n_proj - 1

    # test apply_proj() with and without preload
    for preload in [True, False]:
        raw = read_raw_fif(fif_fname, preload=preload)
        data, times = raw[:, 0:2]
        raw.apply_proj()
        data_proj_1 = np.dot(raw._projector, data)

        # load the file again without proj
        raw = read_raw_fif(fif_fname, preload=preload)

        # write the file with proj. activated, make sure proj has been applied
        raw.save(tmp_path / "raw.fif", proj=True, overwrite=True)
        raw2 = read_raw_fif(tmp_path / "raw.fif")
        data_proj_2, _ = raw2[:, 0:2]
        assert_allclose(data_proj_1, data_proj_2)
        assert all(p["active"] for p in raw2.info["projs"])

        # read orig file with proj. active
        raw2 = read_raw_fif(fif_fname, preload=preload)
        raw2.apply_proj()
        data_proj_2, _ = raw2[:, 0:2]
        assert_allclose(data_proj_1, data_proj_2)
        assert all(p["active"] for p in raw2.info["projs"])

        # test that apply_proj works
        raw.apply_proj()
        data_proj_2, _ = raw[:, 0:2]
        assert_allclose(data_proj_1, data_proj_2)
        assert_allclose(data_proj_2, np.dot(raw._projector, data_proj_2))

    # Test that picking removes projectors ...
    raw = read_raw_fif(fif_fname)
    n_projs = len(raw.info["projs"])
    raw.pick(picks="eeg")
    assert len(raw.info["projs"]) == n_projs - 3

    # ... but only if it doesn't apply to any channels in the dataset anymore.
    raw = read_raw_fif(fif_fname)
    n_projs = len(raw.info["projs"])
    raw.pick(picks=["mag", "eeg"])
    assert len(raw.info["projs"]) == n_projs

    # I/O roundtrip of an MEG projector with a Raw that only contains EEG
    # data.
    out_fname = tmp_path / "test_raw.fif"
    raw = read_raw_fif(test_fif_fname, preload=True).crop(0, 0.002)
    proj = raw.info["projs"][-1]
    raw.pick(picks="eeg")
    raw.add_proj(proj)  # Restore, because picking removed it!
    raw._data.fill(0)
    raw._data[-1] = 1.0
    raw.save(out_fname)
    raw = read_raw_fif(out_fname, preload=False)
    raw.apply_proj()
    assert_allclose(raw[:, :][0][:1], raw[0, :][0])


@testing.requires_testing_data
@pytest.mark.parametrize("preload", [False, True, "memmap2.dat"])
def test_preload_modify(preload, tmp_path):
    """Test preloading and modifying data."""
    rng = np.random.RandomState(0)
    raw = read_raw_fif(fif_fname, preload=preload)

    nsamp = raw.last_samp - raw.first_samp + 1
    picks = pick_types(raw.info, meg="grad", exclude="bads")

    data = rng.randn(len(picks), nsamp // 2)

    try:
        raw[picks, : nsamp // 2] = data
    except RuntimeError:
        if not preload:
            return
        else:
            raise

    tmp_fname = tmp_path / "raw.fif"
    raw.save(tmp_fname, overwrite=True)

    raw_new = read_raw_fif(tmp_fname)
    data_new, _ = raw_new[picks, : nsamp // 2]

    assert_allclose(data, data_new)


@pytest.mark.slowtest
@testing.requires_testing_data
def test_filter():
    """Test filtering (FIR and IIR) and Raw.apply_function interface."""
    raw = read_raw_fif(fif_fname).crop(0, 7)
    raw.load_data()
    sig_dec_notch = 12
    sig_dec_notch_fit = 12
    picks_meg = pick_types(raw.info, meg=True, exclude="bads")
    picks = picks_meg[:4]

    trans = 2.0
    filter_params = dict(
        picks=picks,
        filter_length="auto",
        h_trans_bandwidth=trans,
        l_trans_bandwidth=trans,
        fir_design="firwin",
    )
    raw_lp = raw.copy().filter(None, 8.0, **filter_params)
    raw_hp = raw.copy().filter(16.0, None, **filter_params)
    raw_bp = raw.copy().filter(8.0 + trans, 16.0 - trans, **filter_params)
    raw_bs = raw.copy().filter(16.0, 8.0, **filter_params)

    data, _ = raw[picks, :]

    lp_data, _ = raw_lp[picks, :]
    hp_data, _ = raw_hp[picks, :]
    bp_data, _ = raw_bp[picks, :]
    bs_data, _ = raw_bs[picks, :]

    tols = dict(atol=1e-20, rtol=1e-5)
    assert_allclose(bs_data, lp_data + hp_data, **tols)
    assert_allclose(data, lp_data + bp_data + hp_data, **tols)
    assert_allclose(data, bp_data + bs_data, **tols)

    filter_params_iir = dict(
        picks=picks, n_jobs=2, method="iir", iir_params=dict(output="ba")
    )
    raw_lp_iir = raw.copy().filter(None, 4.0, **filter_params_iir)
    raw_hp_iir = raw.copy().filter(8.0, None, **filter_params_iir)
    raw_bp_iir = raw.copy().filter(4.0, 8.0, **filter_params_iir)
    del filter_params_iir
    lp_data_iir, _ = raw_lp_iir[picks, :]
    hp_data_iir, _ = raw_hp_iir[picks, :]
    bp_data_iir, _ = raw_bp_iir[picks, :]
    summation = lp_data_iir + hp_data_iir + bp_data_iir
    assert_array_almost_equal(data[:, 100:-100], summation[:, 100:-100], 11)

    # make sure we didn't touch other channels
    data, _ = raw[picks_meg[4:], :]
    bp_data, _ = raw_bp[picks_meg[4:], :]
    assert_array_equal(data, bp_data)
    bp_data_iir, _ = raw_bp_iir[picks_meg[4:], :]
    assert_array_equal(data, bp_data_iir)

    # ... and that inplace changes are inplace
    raw_copy = raw.copy()
    assert np.may_share_memory(raw._data, raw._data)
    assert not np.may_share_memory(raw_copy._data, raw._data)
    # this could be assert_array_equal but we do this to mirror the call below
    assert (raw._data[0] == raw_copy._data[0]).all()
    raw_copy.filter(None, 20.0, n_jobs=2, **filter_params)
    assert not (raw._data[0] == raw_copy._data[0]).all()
    assert_array_equal(
        raw.copy().filter(None, 20.0, **filter_params)._data, raw_copy._data
    )

    # do a very simple check on line filtering
    raw_bs = raw.copy().filter(60.0 + trans, 60.0 - trans, **filter_params)
    data_bs, _ = raw_bs[picks, :]
    raw_notch = raw.copy().notch_filter(
        60.0, picks=picks, n_jobs=2, method="fir", trans_bandwidth=2 * trans
    )
    data_notch, _ = raw_notch[picks, :]
    assert_array_almost_equal(data_bs, data_notch, sig_dec_notch)

    # now use the sinusoidal fitting
    assert raw.times[-1] < 10  # catch error with filter_length > n_times
    raw_notch = raw.copy().notch_filter(
        None, picks=picks, n_jobs=2, method="spectrum_fit", filter_length="10s"
    )
    data_notch, _ = raw_notch[picks, :]
    data, _ = raw[picks, :]
    assert_array_almost_equal(data, data_notch, sig_dec_notch_fit)

    # filter should set the "lowpass" and "highpass" parameters
    raw = RawArray(
        np.random.randn(3, 1000), create_info(3, 1000.0, ["eeg"] * 2 + ["stim"])
    )
    with raw.info._unlock():
        raw.info["lowpass"] = raw.info["highpass"] = None
    for kind in ("none", "lowpass", "highpass", "bandpass", "bandstop"):
        print(kind)
        h_freq = l_freq = None
        if kind in ("lowpass", "bandpass"):
            h_freq = 70
        if kind in ("highpass", "bandpass"):
            l_freq = 30
        if kind == "bandstop":
            l_freq, h_freq = 70, 30
        assert raw.info["lowpass"] is None
        assert raw.info["highpass"] is None
        kwargs = dict(
            l_trans_bandwidth=20,
            h_trans_bandwidth=20,
            filter_length="auto",
            phase="zero",
            fir_design="firwin",
        )
        raw_filt = raw.copy().filter(l_freq, h_freq, picks=np.arange(1), **kwargs)
        assert raw.info["lowpass"] is None
        assert raw.info["highpass"] is None
        raw_filt = raw.copy().filter(l_freq, h_freq, **kwargs)
        wanted_h = h_freq if kind != "bandstop" else None
        wanted_l = l_freq if kind != "bandstop" else None
        assert raw_filt.info["lowpass"] == wanted_h
        assert raw_filt.info["highpass"] == wanted_l
        # Using all data channels should still set the params (GH#3259)
        raw_filt = raw.copy().filter(l_freq, h_freq, picks=np.arange(2), **kwargs)
        assert raw_filt.info["lowpass"] == wanted_h
        assert raw_filt.info["highpass"] == wanted_l


def test_filter_picks():
    """Test filtering default channel picks."""
    ch_types = [
        "mag",
        "grad",
        "eeg",
        "seeg",
        "dbs",
        "misc",
        "stim",
        "ecog",
        "hbo",
        "hbr",
    ]
    info = create_info(ch_names=ch_types, ch_types=ch_types, sfreq=256)
    raw = RawArray(data=np.zeros((len(ch_types), 1000)), info=info)

    # -- Deal with meg mag grad and fnirs exceptions
    ch_types = ("misc", "stim", "meg", "eeg", "seeg", "dbs", "ecog")

    # -- Filter data channels
    for ch_type in ("mag", "grad", "eeg", "seeg", "dbs", "ecog", "hbo", "hbr"):
        picks = {ch: ch == ch_type for ch in ch_types}
        picks["meg"] = ch_type if ch_type in ("mag", "grad") else False
        picks["fnirs"] = ch_type if ch_type in ("hbo", "hbr") else False
        raw_ = raw.copy().pick_types(**picks)
        raw_.filter(10, 30, fir_design="firwin")

    # -- Error if no data channel
    for ch_type in ("misc", "stim"):
        picks = {ch: ch == ch_type for ch in ch_types}
        raw_ = raw.copy().pick_types(**picks)
        pytest.raises(ValueError, raw_.filter, 10, 30)


@testing.requires_testing_data
def test_crop():
    """Test cropping raw files."""
    # split a concatenated file to test a difficult case
    raw = concatenate_raws([read_raw_fif(f) for f in [fif_fname, fif_fname]])
    split_size = 10.0  # in seconds
    sfreq = raw.info["sfreq"]
    nsamp = raw.last_samp - raw.first_samp + 1

    # do an annoying case (off-by-one splitting)
    tmins = np.r_[1.0, np.round(np.arange(0.0, nsamp - 1, split_size * sfreq))]
    tmins = np.sort(tmins)
    tmaxs = np.concatenate((tmins[1:] - 1, [nsamp - 1]))
    tmaxs /= sfreq
    tmins /= sfreq
    raws = [None] * len(tmins)
    for ri, (tmin, tmax) in enumerate(zip(tmins, tmaxs)):
        raws[ri] = raw.copy().crop(tmin, tmax)
        if ri < len(tmins) - 1:
            assert_allclose(
                raws[ri].times,
                raw.copy().crop(tmin, tmins[ri + 1], include_tmax=False).times,
            )
        assert raws[ri]
    all_raw_2 = concatenate_raws(raws, preload=False)
    assert raw.first_samp == all_raw_2.first_samp
    assert raw.last_samp == all_raw_2.last_samp
    assert_array_equal(raw[:, :][0], all_raw_2[:, :][0])

    tmins = np.round(np.arange(0.0, nsamp - 1, split_size * sfreq))
    tmaxs = np.concatenate((tmins[1:] - 1, [nsamp - 1]))
    tmaxs /= sfreq
    tmins /= sfreq

    # going in reverse order so the last fname is the first file (need it
    # later)
    raws = [None] * len(tmins)
    for ri, (tmin, tmax) in enumerate(zip(tmins, tmaxs)):
        raws[ri] = raw.copy().crop(tmin, tmax)
    # test concatenation of split file
    all_raw_1 = concatenate_raws(raws, preload=False)

    all_raw_2 = raw.copy().crop(0, None)
    for ar in [all_raw_1, all_raw_2]:
        assert raw.first_samp == ar.first_samp
        assert raw.last_samp == ar.last_samp
        assert_array_equal(raw[:, :][0], ar[:, :][0])

    # test shape consistency of cropped raw
    data = np.zeros((1, 1002001))
    info = create_info(1, 1000)
    raw = RawArray(data, info)
    for tmin in range(0, 1001, 100):
        raw1 = raw.copy().crop(tmin=tmin, tmax=tmin + 2)
        assert raw1[:][0].shape == (1, 2001)

    # degenerate
    with pytest.raises(ValueError, match="No samples.*when include_tmax=False"):
        raw.crop(0, 0, include_tmax=False)

    # edge cases cropping to exact duration +/- 1 sample
    data = np.zeros((1, 100))
    info = create_info(1, 100)
    raw = RawArray(data, info)
    with pytest.raises(ValueError, match="tmax \\(1\\) must be less than or "):
        raw.copy().crop(tmax=1, include_tmax=True)
    raw1 = raw.copy().crop(tmax=1 - 1 / raw.info["sfreq"], include_tmax=True)
    assert raw.n_times == raw1.n_times
    raw2 = raw.copy().crop(tmax=1, include_tmax=False)
    assert raw.n_times == raw2.n_times
    raw3 = raw.copy().crop(tmax=1 - 1 / raw.info["sfreq"], include_tmax=False)
    assert raw.n_times - 1 == raw3.n_times


@testing.requires_testing_data
def test_resample_with_events():
    """Test resampling raws with events."""
    raw = read_raw_fif(fif_fname)
    raw.resample(250)  # pretend raw is recorded at 250 Hz
    events, _ = events_from_annotations(raw)
    raw, events = raw.resample(250, events=events)


@testing.requires_testing_data
def test_resample_equiv():
    """Test resample (with I/O and multiple files)."""
    raw = read_raw_fif(fif_fname).crop(0, 1)
    raw_preload = raw.copy().load_data()
    for r in (raw, raw_preload):
        r.resample(r.info["sfreq"] / 4.0)
    assert_allclose(raw._data, raw_preload._data)


@pytest.mark.slowtest
@testing.requires_testing_data
@pytest.mark.parametrize(
    "preload, n, npad, method",
    [
        (True, 512, "auto", "fft"),
        (True, 512, "auto", "polyphase"),
        (False, 512, 0, "fft"),  # only test one with non-preload because it's slow
    ],
)
def test_resample(tmp_path, preload, n, npad, method):
    """Test resample (with I/O and multiple files)."""
    kwargs = dict(npad=npad, method=method)
    raw = read_raw_fif(fif_fname)
    raw.crop(0, raw.times[n - 1])
    # Reduce to a few MEG channels and a few stim channels to speed up
    n_meg = 5
    raw.pick(raw.ch_names[:n_meg] + raw.ch_names[312:320])  # 10 MEG + 3 STIM + 5 EEG
    assert len(raw.times) == n
    if preload:
        raw.load_data()
    raw_resamp = raw.copy()
    sfreq = raw.info["sfreq"]
    # test parallel on upsample
    raw_resamp.resample(sfreq * 2, n_jobs=2, **kwargs)
    assert raw_resamp.n_times == len(raw_resamp.times)
    raw_resamp.save(tmp_path / "raw_resamp-raw.fif")
    raw_resamp = read_raw_fif(tmp_path / "raw_resamp-raw.fif", preload=True)
    assert sfreq == raw_resamp.info["sfreq"] / 2
    assert raw.n_times == raw_resamp.n_times // 2
    assert raw_resamp.get_data().shape[1] == raw_resamp.n_times
    assert raw.get_data().shape[0] == raw_resamp._data.shape[0]
    # test non-parallel on downsample
    with catch_logging() as log:
        raw_resamp.resample(sfreq, n_jobs=None, verbose=True, **kwargs)
    log = log.getvalue()
    if method == "fft":
        assert "neighborhood" not in log
    else:
        assert "neighborhood" in log
    assert raw_resamp.info["sfreq"] == sfreq
    assert raw.get_data().shape == raw_resamp._data.shape
    assert raw.first_samp == raw_resamp.first_samp
    assert raw.last_samp == raw.last_samp
    # upsampling then downsampling doubles resampling error, but this still
    # works (hooray). Note that the stim channels had to be sub-sampled
    # without filtering to be accurately preserved
    # note we have to treat MEG and EEG+STIM channels differently (tols)
    want_meg = raw.get_data()[:n_meg, 200:-200]
    got_meg = raw_resamp._data[:n_meg, 200:-200]
    want_non_meg = raw.get_data()[n_meg:, 200:-200]
    got_non_meg = raw_resamp._data[n_meg:, 200:-200]
    assert_allclose(got_meg, want_meg, rtol=1e-2, atol=1e-12)
    assert_allclose(want_non_meg, got_non_meg, rtol=1e-2, atol=1e-7)

    # now check multiple file support w/resampling, as order of operations
    # (concat, resample) should not affect our data
    raw1 = raw.copy()
    raw2 = raw.copy()
    raw3 = raw.copy()
    raw4 = raw.copy()
    raw1 = concatenate_raws([raw1, raw2])
    raw1.resample(10.0, **kwargs)
    raw3.resample(10.0, **kwargs)
    raw4.resample(10.0, **kwargs)
    raw3 = concatenate_raws([raw3, raw4])
    assert_array_equal(raw1._data, raw3._data)
    assert_array_equal(raw1._first_samps, raw3._first_samps)
    assert_array_equal(raw1._last_samps, raw3._last_samps)
    assert_array_equal(raw1._raw_lengths, raw3._raw_lengths)
    assert raw1.first_samp == raw3.first_samp
    assert raw1.last_samp == raw3.last_samp
    assert raw1.info["sfreq"] == raw3.info["sfreq"]

    # smoke test crop after resample
    raw4.crop(tmin=raw4.times[1], tmax=raw4.times[-1])

    # test resampling of stim channel

    # basic decimation
    stim = [1, 1, 1, 1, 0, 0, 0, 0, 1, 1, 1, 1, 0, 0, 0, 0]
    raw = RawArray([stim], create_info(1, len(stim), ["stim"]))
    assert_allclose(raw.resample(8.0, **kwargs)._data, [[1, 1, 0, 0, 1, 1, 0, 0]])

    # decimation of multiple stim channels
    raw = RawArray(2 * [stim], create_info(2, len(stim), 2 * ["stim"]))
    assert_allclose(
        raw.resample(8.0, **kwargs, verbose="error")._data,
        [[1, 1, 0, 0, 1, 1, 0, 0], [1, 1, 0, 0, 1, 1, 0, 0]],
    )

    # decimation that could potentially drop events if the decimation is
    # done naively
    stim = [0, 0, 0, 1, 1, 0, 0, 0]
    raw = RawArray([stim], create_info(1, len(stim), ["stim"]))
    assert_allclose(raw.resample(4.0, **kwargs)._data, [[0, 1, 1, 0]])

    # two events are merged in this case (warning)
    stim = [0, 0, 1, 1, 1, 0, 0, 1, 1, 1, 1, 0, 0, 0, 0, 0]
    raw = RawArray([stim], create_info(1, len(stim), ["stim"]))
    with pytest.warns(RuntimeWarning, match="become unreliable"):
        raw.resample(8.0, **kwargs)

    # events are dropped in this case (warning)
    stim = [0, 1, 1, 0, 0, 1, 1, 0]
    raw = RawArray([stim], create_info(1, len(stim), ["stim"]))
    with pytest.warns(RuntimeWarning, match="become unreliable"):
        raw.resample(4.0, **kwargs)

    # test resampling events: this should no longer give a warning
    # we often have first_samp != 0, include it here too
    stim = [0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0, 0, 1]  # an event at end
    # test is on half the sfreq, but should work with trickier ones too
    o_sfreq, sfreq_ratio = len(stim), 0.5
    n_sfreq = o_sfreq * sfreq_ratio
    first_samp = len(stim) // 2
    raw = RawArray([stim], create_info(1, o_sfreq, ["stim"]), first_samp=first_samp)
    events = find_events(raw)
    raw, events = raw.resample(n_sfreq, events=events, **kwargs)
    # Try index into raw.times with resampled events:
    raw.times[events[:, 0] - raw.first_samp]
    n_fsamp = int(first_samp * sfreq_ratio)  # how it's calc'd in base.py
    # NB np.round used for rounding event times, which has 0.5 as corner case:
    # https://docs.scipy.org/doc/numpy/reference/generated/numpy.around.html
    assert_array_equal(
        events,
        np.array(
            [
                [np.round(1 * sfreq_ratio) + n_fsamp, 0, 1],
                [np.round(10 * sfreq_ratio) + n_fsamp, 0, 1],
                [
                    np.minimum(np.round(15 * sfreq_ratio), raw._data.shape[1] - 1)
                    + n_fsamp,
                    0,
                    1,
                ],
            ]
        ),
    )

    # test copy flag
    stim = [1, 1, 1, 1, 0, 0, 0, 0, 1, 1, 1, 1, 0, 0, 0, 0]
    raw = RawArray([stim], create_info(1, len(stim), ["stim"]))
    raw_resampled = raw.copy().resample(4.0, **kwargs)
    assert raw_resampled is not raw
    raw_resampled = raw.resample(4.0, **kwargs)
    assert raw_resampled is raw

    # resample should still work even when no stim channel is present
    raw = RawArray(np.random.randn(1, 100), create_info(1, 100, ["eeg"]))
    with raw.info._unlock():
        raw.info["lowpass"] = 50.0
    raw.resample(10, **kwargs)
    assert raw.info["lowpass"] == 5.0
    assert len(raw) == 10


def test_resample_stim():
    """Test stim_picks argument."""
    data = np.ones((2, 1000))
    info = create_info(2, 1000.0, ("eeg", "misc"))
    raw = RawArray(data, info)
    raw.resample(500.0, stim_picks="misc")


@testing.requires_testing_data
def test_hilbert():
    """Test computation of analytic signal using hilbert."""
    raw = read_raw_fif(fif_fname, preload=True)
    picks_meg = pick_types(raw.info, meg=True, exclude="bads")
    picks = picks_meg[:4]

    raw_filt = raw.copy()
    raw_filt.filter(
        10,
        20,
        picks=picks,
        l_trans_bandwidth="auto",
        h_trans_bandwidth="auto",
        filter_length="auto",
        phase="zero",
        fir_window="blackman",
        fir_design="firwin",
    )
    raw_filt_2 = raw_filt.copy()

    raw2 = raw.copy()
    raw3 = raw.copy()
    raw.apply_hilbert(picks, n_fft="auto")
    raw2.apply_hilbert(picks, n_fft="auto", envelope=True)

    # Test custom n_fft
    raw_filt.apply_hilbert(picks, n_fft="auto")
    n_fft = 2 ** int(np.ceil(np.log2(raw_filt_2.n_times + 1000)))
    raw_filt_2.apply_hilbert(picks, n_fft=n_fft)
    assert raw_filt._data.shape == raw_filt_2._data.shape
    assert_allclose(
        raw_filt._data[:, 50:-50], raw_filt_2._data[:, 50:-50], atol=1e-13, rtol=1e-2
    )
    with pytest.raises(ValueError, match="n_fft.*must be at least the number"):
        raw3.apply_hilbert(picks, n_fft=raw3.n_times - 100)

    env = np.abs(raw._data[picks, :])
    assert_allclose(env, raw2._data[picks, :], rtol=1e-2, atol=1e-13)


@testing.requires_testing_data
def test_raw_copy():
    """Test Raw copy."""
    raw = read_raw_fif(fif_fname, preload=True)
    data, _ = raw[:, :]
    copied = raw.copy()
    copied_data, _ = copied[:, :]
    assert_array_equal(data, copied_data)
    assert sorted(raw.__dict__.keys()) == sorted(copied.__dict__.keys())

    raw = read_raw_fif(fif_fname, preload=False)
    data, _ = raw[:, :]
    copied = raw.copy()
    copied_data, _ = copied[:, :]
    assert_array_equal(data, copied_data)
    assert sorted(raw.__dict__.keys()) == sorted(copied.__dict__.keys())


def test_to_data_frame():
    """Test raw Pandas exporter."""
    pd = pytest.importorskip("pandas")
    raw = read_raw_fif(test_fif_fname).crop(0, 1).load_data()
    df = raw.to_data_frame(index="time")
    assert (df.columns == raw.ch_names).all()
    df = raw.to_data_frame(index=None)
    assert "time" in df.columns
    assert_array_equal(df.values[:, 1], raw._data[0] * 1e13)
    assert_array_equal(df.values[:, 3], raw._data[2] * 1e15)
    # test long format
    df_long = raw.to_data_frame(long_format=True)
    assert len(df_long) == raw.get_data().size
    expected = ("time", "channel", "ch_type", "value")
    assert set(expected) == set(df_long.columns)
    # test bad time format
    with pytest.raises(ValueError, match="not a valid time format. Valid"):
        raw.to_data_frame(time_format="foo")
    # test time format error handling
    raw.set_meas_date(None)
    with pytest.warns(RuntimeWarning, match="Cannot convert to Datetime when"):
        df = raw.to_data_frame(time_format="datetime")
    assert isinstance(df["time"].iloc[0], pd.Timedelta)


@pytest.mark.parametrize("time_format", (None, "ms", "timedelta", "datetime"))
def test_to_data_frame_time_format(time_format):
    """Test time conversion in epochs Pandas exporter."""
    pd = pytest.importorskip("pandas")
    raw = read_raw_fif(test_fif_fname, preload=True)
    # test time_format
    df = raw.to_data_frame(time_format=time_format)
    dtypes = {
        None: np.float64,
        "ms": np.int64,
        "timedelta": pd.Timedelta,
        "datetime": pd.Timestamp,
    }
    assert isinstance(df["time"].iloc[0], dtypes[time_format])
    # test values
    _, times = raw[0, :10]
    offset = 0.0
    if time_format == "datetime":
        times += raw.first_time
        offset = raw.info["meas_date"]
    elif time_format == "timedelta":
        offset = pd.Timedelta(0.0)
    funcs = {
        None: lambda x: x,
        "ms": lambda x: np.rint(x * 1e3).astype(int),  # s → ms
        "timedelta": partial(pd.to_timedelta, unit="s"),
        "datetime": partial(pd.to_timedelta, unit="s"),
    }
    assert_array_equal(funcs[time_format](times) + offset, df["time"][:10])


def test_add_channels():
    """Test raw splitting / re-appending channel types."""
    rng = np.random.RandomState(0)
    raw = read_raw_fif(test_fif_fname).crop(0, 1).load_data()
    assert raw._orig_units == {}
    raw_nopre = read_raw_fif(test_fif_fname, preload=False)
    raw_eeg_meg = raw.copy().pick(picks=["meg", "eeg"])
    raw_eeg = raw.copy().pick(picks="eeg")
    raw_meg = raw.copy().pick(picks="meg")
    raw_stim = raw.copy().pick(picks="stim")
    raw_new = raw_meg.copy().add_channels([raw_eeg, raw_stim])
    assert all(
        ch in raw_new.ch_names
        for ch in list(raw_stim.ch_names) + list(raw_meg.ch_names)
    )
    raw_new = raw_meg.copy().add_channels([raw_eeg])

    assert (ch in raw_new.ch_names for ch in raw.ch_names)
    assert_array_equal(raw_new[:, :][0], raw_eeg_meg[:, :][0])
    assert_array_equal(raw_new[:, :][1], raw[:, :][1])
    assert all(ch not in raw_new.ch_names for ch in raw_stim.ch_names)

    # Testing force updates
    raw_arr_info = create_info(["1", "2"], raw_meg.info["sfreq"], "eeg")
    assert raw_arr_info["dev_head_t"] is None
    orig_head_t = Transform("meg", "head")
    raw_arr = rng.randn(2, raw_eeg.n_times)
    raw_arr = RawArray(raw_arr, raw_arr_info)
    # This should error because of conflicts in Info
    raw_arr.info["dev_head_t"] = orig_head_t
    with pytest.raises(ValueError, match="mutually inconsistent dev_head_t"):
        raw_meg.copy().add_channels([raw_arr])
    raw_meg.copy().add_channels([raw_arr], force_update_info=True)
    # Make sure that values didn't get overwritten
    assert_object_equal(raw_arr.info["dev_head_t"], orig_head_t)
    # Make sure all variants work
    for simult in (False, True):  # simultaneous adding or not
        raw_new = raw_meg.copy()
        if simult:
            raw_new.add_channels([raw_eeg, raw_stim])
        else:
            raw_new.add_channels([raw_eeg])
            raw_new.add_channels([raw_stim])
        for other in (raw_meg, raw_stim, raw_eeg):
            assert_allclose(
                raw_new.copy().pick(other.ch_names).get_data(),
                other.get_data(),
            )

    # Now test errors
    raw_badsf = raw_eeg.copy()
    with raw_badsf.info._unlock():
        raw_badsf.info["sfreq"] = 3.1415927
    raw_eeg.crop(0.5)

    pytest.raises(RuntimeError, raw_meg.add_channels, [raw_nopre])
    pytest.raises(RuntimeError, raw_meg.add_channels, [raw_badsf])
    pytest.raises(ValueError, raw_meg.add_channels, [raw_eeg])
    pytest.raises(ValueError, raw_meg.add_channels, [raw_meg])
    pytest.raises(TypeError, raw_meg.add_channels, raw_badsf)


@testing.requires_testing_data
def test_save(tmp_path):
    """Test saving raw."""
    temp_fname = tmp_path / "test_raw.fif"
    shutil.copyfile(fif_fname, temp_fname)
    raw = read_raw_fif(temp_fname, preload=False)
    # can't write over file being read
    with pytest.raises(ValueError, match="to the same file"):
        raw.save(temp_fname)
    raw.load_data()
    # can't overwrite file without overwrite=True
    with pytest.raises(OSError, match="file exists"):
        raw.save(fif_fname)

    # test abspath support and annotations
    orig_time = _dt_to_stamp(raw.info["meas_date"])[0] + raw._first_time
    annot = Annotations([10], [5], ["test"], orig_time=orig_time)
    raw.set_annotations(annot)
    annot = raw.annotations
    new_fname = tmp_path / "break_raw.fif"
    raw.save(new_fname, overwrite=True)
    new_raw = read_raw_fif(new_fname, preload=False)
    pytest.raises(ValueError, new_raw.save, new_fname)
    assert_array_almost_equal(annot.onset, new_raw.annotations.onset)
    assert_array_equal(annot.duration, new_raw.annotations.duration)
    assert_array_equal(annot.description, new_raw.annotations.description)
    assert annot.orig_time == new_raw.annotations.orig_time

    # test set_meas_date(None)
    raw.set_meas_date(None)
    raw.save(new_fname, overwrite=True)
    new_raw = read_raw_fif(new_fname, preload=False)
    assert new_raw.info["meas_date"] is None


@testing.requires_testing_data
def test_annotation_crop(tmp_path):
    """Test annotation sync after cropping and concatenating."""
    annot = Annotations([5.0, 11.0, 15.0], [2.0, 1.0, 3.0], ["test", "test", "test"])
    raw = read_raw_fif(fif_fname, preload=False)
    raw.set_annotations(annot)
    r1 = raw.copy().crop(2.5, 7.5)
    r2 = raw.copy().crop(12.5, 17.5)
    r3 = raw.copy().crop(10.0, 12.0)
    raw = concatenate_raws([r1, r2, r3])  # segments reordered
    assert_and_remove_boundary_annot(raw, 2)
    onsets = raw.annotations.onset
    durations = raw.annotations.duration
    # 2*5s clips combined with annotations at 2.5s + 2s clip, annotation at 1s
    assert_array_almost_equal(onsets[:3], [47.95, 52.95, 56.46], decimal=2)
    assert_array_almost_equal([2.0, 2.5, 1.0], durations[:3], decimal=2)

    # test annotation clipping
    orig_time = _dt_to_stamp(raw.info["meas_date"])
    orig_time = orig_time[0] + orig_time[1] * 1e-6 + raw._first_time - 1.0
    annot = Annotations([0.0, raw.times[-1]], [2.0, 2.0], "test", orig_time)
    with pytest.warns(RuntimeWarning, match="Limited .* expanding outside"):
        raw.set_annotations(annot)
    assert_allclose(
        raw.annotations.duration, [1.0, 1.0 + 1.0 / raw.info["sfreq"]], atol=1e-3
    )

    # make sure we can overwrite the file we loaded when preload=True
    new_fname = tmp_path / "break_raw.fif"
    raw.save(new_fname)
    new_raw = read_raw_fif(new_fname, preload=True)
    new_raw.save(new_fname, overwrite=True)


@testing.requires_testing_data
def test_with_statement():
    """Test with statement."""
    for preload in [True, False]:
        with read_raw_fif(fif_fname, preload=preload) as raw_:
            print(raw_)


def test_compensation_raw(tmp_path):
    """Test Raw compensation."""
    raw_3 = read_raw_fif(ctf_comp_fname)
    assert raw_3.compensation_grade == 3
    data_3, times = raw_3[:, :]

    # data come with grade 3
    for ii in range(2):
        raw_3_new = raw_3.copy()
        if ii == 0:
            raw_3_new.load_data()
        raw_3_new.apply_gradient_compensation(3)
        assert raw_3_new.compensation_grade == 3
        data_new, times_new = raw_3_new[:, :]
        assert_array_equal(times, times_new)
        assert_array_equal(data_3, data_new)

    # change to grade 0
    raw_0 = raw_3.copy().apply_gradient_compensation(0)
    assert raw_0.compensation_grade == 0
    data_0, times_new = raw_0[:, :]
    assert_array_equal(times, times_new)
    assert np.mean(np.abs(data_0 - data_3)) > 1e-12
    # change to grade 1
    raw_1 = raw_0.copy().apply_gradient_compensation(1)
    assert raw_1.compensation_grade == 1
    data_1, times_new = raw_1[:, :]
    assert_array_equal(times, times_new)
    assert np.mean(np.abs(data_1 - data_3)) > 1e-12
    pytest.raises(ValueError, raw_1.apply_gradient_compensation, 33)
    raw_bad = raw_0.copy()
    raw_bad.add_proj(compute_proj_raw(raw_0, duration=0.5, verbose="error"))
    raw_bad.apply_proj()
    pytest.raises(RuntimeError, raw_bad.apply_gradient_compensation, 1)
    # with preload
    tols = dict(rtol=1e-12, atol=1e-25)
    raw_1_new = raw_3.copy().load_data().apply_gradient_compensation(1)
    assert raw_1_new.compensation_grade == 1
    data_1_new, times_new = raw_1_new[:, :]
    assert_array_equal(times, times_new)
    assert np.mean(np.abs(data_1_new - data_3)) > 1e-12
    assert_allclose(data_1, data_1_new, **tols)
    # change back
    raw_3_new = raw_1.copy().apply_gradient_compensation(3)
    data_3_new, times_new = raw_3_new[:, :]
    assert_allclose(data_3, data_3_new, **tols)
    raw_3_new = raw_1.copy().load_data().apply_gradient_compensation(3)
    data_3_new, times_new = raw_3_new[:, :]
    assert_allclose(data_3, data_3_new, **tols)

    for load in (False, True):
        for raw in (raw_0, raw_1):
            raw_3_new = raw.copy()
            if load:
                raw_3_new.load_data()
            raw_3_new.apply_gradient_compensation(3)
            assert raw_3_new.compensation_grade == 3
            data_3_new, times_new = raw_3_new[:, :]
            assert_array_equal(times, times_new)
            assert np.mean(np.abs(data_3_new - data_1)) > 1e-12
            assert_allclose(data_3, data_3_new, **tols)

    # Try IO with compensation
    temp_file = tmp_path / "raw.fif"
    raw_3.save(temp_file, overwrite=True)
    for preload in (True, False):
        raw_read = read_raw_fif(temp_file, preload=preload)
        assert raw_read.compensation_grade == 3
        data_read, times_new = raw_read[:, :]
        assert_array_equal(times, times_new)
        assert_allclose(data_3, data_read, **tols)
        raw_read.apply_gradient_compensation(1)
        data_read, times_new = raw_read[:, :]
        assert_array_equal(times, times_new)
        assert_allclose(data_1, data_read, **tols)

    # Now save the file that has modified compensation
    # and make sure the compensation is the same as it was,
    # but that we can undo it

    # These channels have norm 1e-11/1e-12, so atol=1e-18 isn't awesome,
    # but it's due to the single precision of the info['comps'] leading
    # to inexact inversions with saving/loading (casting back to single)
    # in between (e.g., 1->3->1 will degrade like this)
    looser_tols = dict(rtol=1e-6, atol=1e-18)
    raw_1.save(temp_file, overwrite=True)
    for preload in (True, False):
        raw_read = read_raw_fif(temp_file, preload=preload, verbose=True)
        assert raw_read.compensation_grade == 1
        data_read, times_new = raw_read[:, :]
        assert_array_equal(times, times_new)
        assert_allclose(data_1, data_read, **looser_tols)
        raw_read.apply_gradient_compensation(3, verbose=True)
        data_read, times_new = raw_read[:, :]
        assert_array_equal(times, times_new)
        assert_allclose(data_3, data_read, **looser_tols)


@requires_mne
def test_compensation_raw_mne(tmp_path):
    """Test Raw compensation by comparing with MNE-C."""

    def compensate_mne(fname, grad):
        tmp_fname = tmp_path / "mne_ctf_test_raw.fif"
        cmd = [
            "mne_process_raw",
            "--raw",
            fname,
            "--save",
            tmp_fname,
            "--grad",
            str(grad),
            "--projoff",
            "--filteroff",
        ]
        run_subprocess(cmd)
        return read_raw_fif(tmp_fname, preload=True)

    for grad in [0, 2, 3]:
        raw_py = read_raw_fif(ctf_comp_fname, preload=True)
        raw_py.apply_gradient_compensation(grad)
        raw_c = compensate_mne(ctf_comp_fname, grad)
        assert_allclose(raw_py._data, raw_c._data, rtol=1e-6, atol=1e-17)
        assert raw_py.info["nchan"] == raw_c.info["nchan"]
        for ch_py, ch_c in zip(raw_py.info["chs"], raw_c.info["chs"]):
            for key in (
                "ch_name",
                "coil_type",
                "scanno",
                "logno",
                "unit",
                "coord_frame",
                "kind",
            ):
                assert ch_py[key] == ch_c[key]
            for key in ("loc", "unit_mul", "range", "cal"):
                assert_allclose(ch_py[key], ch_c[key])


@testing.requires_testing_data
def test_drop_channels_mixin():
    """Test channels-dropping functionality."""
    raw = read_raw_fif(fif_fname, preload=True)
    drop_ch = raw.ch_names[:3]
    ch_names = raw.ch_names[3:]

    ch_names_orig = raw.ch_names
    dummy = raw.copy().drop_channels(drop_ch)
    assert ch_names == dummy.ch_names
    assert ch_names_orig == raw.ch_names
    assert len(ch_names_orig) == raw._data.shape[0]

    raw.drop_channels(drop_ch)
    assert ch_names == raw.ch_names
    assert len(ch_names) == len(raw._cals)
    assert len(ch_names) == raw._data.shape[0]

    # Test that dropping all channels a projector applies to will lead to the
    # removal of said projector.
    raw = read_raw_fif(fif_fname).crop(0, 1)
    n_projs = len(raw.info["projs"])
    eeg_names = raw.info["projs"][-1]["data"]["col_names"]
    with pytest.raises(RuntimeError, match="loaded"):
        raw.copy().apply_proj().drop_channels(eeg_names)
    raw.load_data().drop_channels(eeg_names)  # EEG proj
    assert len(raw.info["projs"]) == n_projs - 1

    # Dropping EEG channels with custom ref removes info['custom_ref_applied']
    raw = read_raw_fif(fif_fname).crop(0, 1).load_data()
    raw.set_eeg_reference()
    assert raw.info["custom_ref_applied"]
    raw.drop_channels(eeg_names)
    assert not raw.info["custom_ref_applied"]


@testing.requires_testing_data
@pytest.mark.parametrize("preload", (True, False))
def test_pick_channels_mixin(preload):
    """Test channel-picking functionality."""
    raw = read_raw_fif(fif_fname, preload=preload)
    raw_orig = raw.copy()
    ch_names = raw.ch_names[:3]

    ch_names_orig = raw.ch_names
    dummy = raw.copy().pick(ch_names)
    assert ch_names == dummy.ch_names
    assert ch_names_orig == raw.ch_names
    assert len(ch_names_orig) == raw.get_data().shape[0]

    raw.pick(ch_names)  # copy is False
    assert ch_names == raw.ch_names
    assert len(ch_names) == len(raw._cals)
    assert len(ch_names) == raw.get_data().shape[0]
    with pytest.raises(ValueError, match='must be list, tuple, ndarray, or "bads"'):
        raw.pick_channels(ch_names[0])  # legacy method OK here; testing its warning

    assert_allclose(raw[:][0], raw_orig[:3][0])


@testing.requires_testing_data
def test_equalize_channels():
    """Test equalization of channels."""
    raw1 = read_raw_fif(fif_fname, preload=True)

    raw2 = raw1.copy()
    ch_names = raw1.ch_names[2:]
    raw1.drop_channels(raw1.ch_names[:1])
    raw2.drop_channels(raw2.ch_names[1:2])
    my_comparison = [raw1, raw2]
    my_comparison = equalize_channels(my_comparison)
    for e in my_comparison:
        assert ch_names == e.ch_names


def test_memmap(tmp_path):
    """Test some interesting memmapping cases."""
    # concatenate_raw
    memmaps = [str(tmp_path / str(ii)) for ii in range(4)]
    raw_0 = read_raw_fif(test_fif_fname, preload=memmaps[0])
    assert raw_0._data.filename == memmaps[0]
    raw_1 = read_raw_fif(test_fif_fname, preload=memmaps[1])
    assert raw_1._data.filename == memmaps[1]
    raw_0.append(raw_1, preload=memmaps[2])
    assert raw_0._data.filename == memmaps[2]
    # add_channels
    orig_data = raw_0[:][0]
    new_ch_info = pick_info(raw_0.info, [0])
    new_ch_info["chs"][0]["ch_name"] = "foo"
    new_ch_info._update_redundant()
    new_data = np.linspace(0, 1, len(raw_0.times))[np.newaxis]
    ch = RawArray(new_data, new_ch_info)
    raw_0.add_channels([ch])
    if platform.system() == "Darwin":
        assert not hasattr(raw_0._data, "filename")
    else:
        assert raw_0._data.filename == memmaps[2]
    assert_allclose(orig_data, raw_0[:-1][0], atol=1e-7)
    assert_allclose(new_data, raw_0[-1][0], atol=1e-7)

    # now let's see if .copy() actually works; it does, but eventually
    # we should make it optionally memmap to a new filename rather than
    # create an in-memory version (filename=None)
    raw_0 = read_raw_fif(test_fif_fname, preload=memmaps[3])
    assert raw_0._data.filename == memmaps[3]
    assert raw_0._data[:1, 3:5].all()
    raw_1 = raw_0.copy()
    assert isinstance(raw_1._data, np.memmap)
    assert raw_1._data.filename is None
    raw_0._data[:] = 0.0
    assert not raw_0._data.any()
    assert raw_1._data[:1, 3:5].all()
    # other things like drop_channels and crop work but do not use memmapping,
    # eventually we might want to add support for some of these as users
    # require them.


# These are slow on Azure Windows so let's do a subset
@pytest.mark.parametrize(
    "kind",
    ["path", pytest.param("file", id="kindFile"), "bytes"],
)
@pytest.mark.parametrize(
    "preload",
    [pytest.param(True, id="preloadTrue"), str],
)
@pytest.mark.parametrize(
    "split",
    [False, pytest.param(True, marks=pytest.mark.slowtest, id="splitTrue")],
)
def test_file_like(kind, preload, split, tmp_path):
    """Test handling with file-like objects."""
    fname = tmp_path / "test_file_like_raw.fif"
    fnames = (fname,)
    this_raw = read_raw_fif(test_fif_fname).crop(0, 4).pick("mag")
    if split:
        this_raw.save(fname, split_size="5MB")
        fnames += (Path(str(fname)[:-4] + "-1.fif"),)
        bad_fname = Path(str(fname)[:-4] + "-2.fif")
        assert not bad_fname.is_file()
    else:
        this_raw.save(fname)
    for f in fnames:
        assert f.is_file()
    if preload is str:
        if platform.system() == "Windows":
            pytest.skip("Cannot test preload=str on Windows")
        preload = str(tmp_path / "memmap")
    with open(fname, "rb") as file_fid:
        if kind == "bytes":
            fid = BytesIO(file_fid.read())
        elif kind == "path":
            fid = fname
        else:
            assert kind == "file"
            fid = file_fid
        if kind != "path":
            assert not fid.closed
            with pytest.raises(ValueError, match="preload must be used with file"):
                read_raw_fif(fid)
        assert not file_fid.closed
        if kind != "path":
            assert not fid.closed
        assert not file_fid.closed
        # Use test_preloading=False but explicitly pass the preload type
        # so that we don't bother testing preload=False
        kwargs = dict(
            fname=fid,
            preload=preload,
            on_split_missing="warn",
            test_preloading=False,
            test_kwargs=False,
        )
        want_filenames = list(fnames)
        if kind == "bytes":
            # the split file will not be correctly resolved for BytesIO
            want_filenames = [None]
        if split and kind == "bytes":
            ctx = pytest.warns(RuntimeWarning, match="Split raw file detected")
        else:
            ctx = nullcontext()
        with ctx:
            raw = _test_raw_reader(read_raw_fif, **kwargs)
        if kind != "path":
            assert not fid.closed
        assert not file_fid.closed
        want_filenames = tuple(want_filenames)
        assert raw.filenames == want_filenames
        if kind == "bytes":
            assert fname.name not in raw._repr_html_()
        else:
            assert fname.name in raw._repr_html_()
    assert file_fid.closed


def test_str_like():
    """Test handling with str-like objects."""
    fname = pathlib.Path(test_fif_fname)
    raw_path = read_raw_fif(fname, preload=True)
    raw_str = read_raw_fif(test_fif_fname, preload=True)
    assert_allclose(raw_path._data, raw_str._data)


@pytest.mark.parametrize(
    "fname",
    [
        test_fif_fname,
        testing._pytest_param(fif_fname),
        testing._pytest_param(ms_fname),
    ],
)
def test_bad_acq(fname):
    """Test handling of acquisition errors."""
    # see gh-7844
    raw = read_raw_fif(fname, allow_maxshield="yes").load_data()
    with open(fname, "rb") as fid:
        for ent in raw._raw_extras[0]["ent"]:
            tag = _read_tag_header(fid, ent.pos)
            # hack these, others (kind, type) should be correct
            tag.pos, tag.next = ent.pos, ent.next
            assert tag == ent


@testing.requires_testing_data
@pytest.mark.skipif(
    platform.system() not in ("Linux", "Darwin"), reason="Needs proper symlinking"
)
def test_split_symlink(tmp_path):
    """Test split files with symlinks."""
    # regression test for gh-9221
    (tmp_path / "first").mkdir()
    first = tmp_path / "first" / "test_raw.fif"
    raw = read_raw_fif(fif_fname).pick("meg").load_data()
    raw.save(first, buffer_size_sec=1, split_size="10MB", verbose=True)
    second = Path(str(first)[:-4] + "-1.fif")
    assert second.is_file()
    assert not Path(str(first)[:-4] + "-2.fif").is_file()
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    new_first = tmp_path / "a" / "test_raw.fif"
    new_second = tmp_path / "b" / "test_raw-1.fif"
    shutil.move(first, new_first)
    shutil.move(second, new_second)
    os.symlink(new_first, first)
    os.symlink(new_second, second)
    raw_new = read_raw_fif(first)
    assert_allclose(raw_new.get_data(), raw.get_data())


@testing.requires_testing_data
@pytest.mark.parametrize("offset", (0, 1))
def test_corrupted(tmp_path, offset):
    """Test that a corrupted file can still be read."""
    # Must be a file written by Neuromag, not us, since we don't write the dir
    # at the end, so use the skip one (straight from acq).
    raw = read_raw_fif(skip_fname)
    with open(skip_fname, "rb") as fid:
        file_id_tag = read_tag(fid, 0)
        dir_pos_tag = read_tag(fid, file_id_tag.next_pos)
        dirpos = int(dir_pos_tag.data.item())
        assert dirpos == 12641532
        fid.seek(0)
        data = fid.read(dirpos + offset)
    bad_fname = tmp_path / "test_raw.fif"
    with open(bad_fname, "wb") as fid:
        fid.write(data)
    with (
        _record_warnings(),
        pytest.warns(RuntimeWarning, match=".*tag directory.*corrupt.*"),
    ):
        raw_bad = read_raw_fif(bad_fname)
    assert_allclose(raw.get_data(), raw_bad.get_data())


@testing.requires_testing_data
def test_expand_user(tmp_path, monkeypatch):
    """Test that we're expanding `~` before reading and writing."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows

    path_in = Path(fif_fname)
    path_out = tmp_path / path_in.name
    path_home = Path("~") / path_in.name

    shutil.copyfile(src=path_in, dst=path_out)

    raw = read_raw_fif(fname=path_home, preload=True)
    raw.save(fname=path_home, overwrite=True)


@pytest.mark.parametrize("cast", [pathlib.Path, str])
def test_init_kwargs(cast):
    """Test for pull/12843#issuecomment-2380491528."""
    raw = read_raw_fif(cast(test_fif_fname))
    raw2 = read_raw_fif(**raw._init_kwargs)
    for r in (raw, raw2):
        assert isinstance(r._init_kwargs["fname"], pathlib.Path)


@pytest.mark.slowtest
@testing.requires_testing_data
@pytest.mark.parametrize("fname", [ms_fname, tri_fname])
def test_fif_files(fname):
    """Test reading of various FIF files."""
    _test_raw_reader(
        read_raw_fif,
        fname=fname,
        allow_maxshield="yes",
        verbose="error",
        test_kwargs=False,
        test_preloading=False,
    )
