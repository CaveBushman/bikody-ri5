"""Measurement must distinguish successful ACKs from absent evidence."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "measure-performance.py"
spec = importlib.util.spec_from_file_location("measure_performance", SCRIPT)
measure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(measure)


def test_ack_percentiles_count_batches_not_frames():
    report = measure.summarize_ack([
        'Oct 11 ri5 python[42]: Průjezdy a1b2c3d4: 50 rámců, potvrzení serveru 10 ms\n',
        'Průjezdy a1b2c3d4: 1 rámců, potvrzení serveru 200 ms\n',
        'Průjezdy e5f6a7b8: 2 rámců, potvrzení serveru 900 ms !\n',
        'Průjezdy e5f6a7b8: 1 rámců, potvrzení serveru 200 ms !\n',
        'Průjezdy e5f6a7b8: nečekaná chyba odesílatele TimeoutError\n',
        'Průjezdy e5f6a7b8: server smyčku nezná, 2 rámců zahozeno (nepatří do závodu)\n',
    ])
    assert report['frames'] == 54
    assert report['ack_ms']['samples'] == 4
    assert report['ack_ms']['p50'] == 200
    assert report['ack_ms']['p95'] == 900
    assert report['batches_over_200_ms'] == 2
    assert report['ignored_lines'] == 2
    assert report['by_decoder']['a1b2c3d4']['frames'] == 51


def test_no_ack_is_not_reported_as_zero_latency():
    result = subprocess.run([sys.executable, str(SCRIPT), 'ack'],
                            input='Agent is starting\n', text=True, capture_output=True, check=False)
    assert result.returncode == 2
    assert result.stdout == ''
    assert 'Žádné potvrzené dávky' in result.stderr


def test_queue_uses_and_removes_only_its_temporary_directory(tmp_path):
    existing = tmp_path / 'live.sqlite3'
    existing.write_bytes(b'leave live data alone')
    result = subprocess.run([
        sys.executable, str(SCRIPT), 'queue', '--directory', str(tmp_path),
        '--frames', '100', '--rounds', '2', '--iterations', '10',
    ], text=True, capture_output=True, check=True)
    report = json.loads(result.stdout)
    assert report['frames'] == 100
    assert report['measurement'] == 'isolated_queue_count_microbenchmark'
    assert report['median_us_per_read']['maintained_count'] > 0
    assert list(tmp_path.iterdir()) == [existing]
    assert existing.read_bytes() == b'leave live data alone'


@pytest.mark.parametrize('value', ['0', '-1', 'bad', '1000000'])
def test_invalid_benchmark_size_is_rejected(value):
    result = subprocess.run([sys.executable, str(SCRIPT), 'queue', '--frames', value],
                            text=True, capture_output=True, check=False)
    assert result.returncode == 2
    assert result.stdout == ''
