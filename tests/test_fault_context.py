import json
from threading import Thread

import pytest

from script.fault_context import FIELDS, FaultContext
from script.log_runner import LogBudget


class Clock:
    now = 100.

    def __call__(self):
        return self.now


def test_fault_captures_fixed_context_and_ignores_secret_payloads():
    context = FaultContext()
    for sequence in range(100):
        context.observe({'status': 'success', 'sequence': sequence, 'jpeg': b'private-image',
                         'detections': [{'label': 'private'}], 'token': 'secret'})
    assert context.take() is None
    event = {'status': 'dropped', 'reason': 'insufficient_inference_time',
             'sequence': 100, 'age_high_us': 400000, 'queue_checked_us': 800000}
    context.observe(event)
    event['sequence'] = 999
    payload = context.take()
    record = json.loads(payload)
    assert len(record['events']) == 8
    assert record['events'][-1]['sequence'] == 100
    assert record['events'][-1]['reason'] == 'insufficient_inference_time'
    assert 'private' not in payload and 'secret' not in payload
    assert context.status()['attempted'] == 1


def test_rate_limit_coalesces_one_pending_snapshot_and_exact_boundary():
    clock = Clock()
    context = FaultContext(clock=clock)
    context.observe({'status': 'failed'})
    assert context.take() is not None
    for sequence in range(10000):
        context.observe({'status': 'dropped', 'sequence': sequence, 'reason': 'stale_capture'})
    assert context.status()['ring_records'] == 8
    assert context.status()['pending'] and context.status()['coalesced'] == 9999
    clock.now += 299.999
    assert context.take() is None
    clock.now += .001
    payload = json.loads(context.take())
    assert payload['events'][-1]['sequence'] == 0  # Preserve the first trigger, not an overwritten cause.
    assert context.take() is None


@pytest.mark.parametrize('value', [True, -1, 2**64, float('inf'), [], 'secret'])
def test_invalid_numeric_metadata_is_excluded(value):
    context = FaultContext()
    context.observe({'status': 'closed', 'reason': 'secret-token', 'sequence': value})
    result = json.loads(context.take())
    assert 'sequence' not in result['events'][0]
    assert result['events'][0]['reason'] == 'other'
    assert result['invalid_fields_total'] == 1


def test_maximum_records_fit_payload_and_existing_collector_line_budget(tmp_path):
    context = FaultContext()
    for _ in range(8):
        context.observe({'status': 'success', **dict.fromkeys(FIELDS, 2**64 - 1)})
    context.observe({'status': 'dropped', 'reason': 'insufficient_inference_time',
                     **dict.fromkeys(FIELDS, 2**64 - 1)})
    payload = context.take()
    assert len(payload.encode()) <= 4096
    assert json.loads(payload)['events'][-1]['status'] == 'dropped'
    budget = LogBudget(tmp_path / 'logs')
    try:
        budget.line('operating_fault ' + payload)
        raw = (tmp_path / 'logs/runtime.log').read_bytes()
        assert len(raw) <= 8192
        assert not budget.truncated and not budget.write_errors
    finally:
        budget._lock_file.close()


def test_recognition_never_waits_for_contended_lock():
    context = FaultContext()
    context.lock.acquire()
    try:
        def observe():
            context.observe({'status': 'dropped'})
            context.observe(None)
        worker = Thread(target=observe, daemon=True)
        worker.start()
        worker.join(.5)
        assert not worker.is_alive()
        assert context.take() is None
    finally:
        context.lock.release()
    assert context.status()['contention_observed']


def test_unknown_and_large_input_is_bounded_and_never_serialized():
    context = FaultContext()
    for event in [None, [], {'status': []}, {'status': 'x' * 1000000}]:
        context.observe(event)
    assert context.status()['ring_records'] == 0
    assert context.status()['rejected_events'] == 4
    context.observe({'status': 'closed', 'reason': 'secret' * 1000000})
    assert json.loads(context.take())['events'][-1]['reason'] == 'other'


def test_serialization_does_not_hold_event_lock(monkeypatch):
    from script import fault_context
    context = FaultContext()
    context.observe({'status': 'dropped', 'reason': 'stale_capture'})
    encode = json.dumps
    def checked(*args, **kwargs):
        assert context.lock.acquire(blocking=False)
        context.lock.release()
        return encode(*args, **kwargs)
    monkeypatch.setattr(fault_context.json, 'dumps', checked)
    assert context.take() is not None


def test_diagnostic_counters_saturate_instead_of_growing_without_bound():
    context = FaultContext()
    context.invalid = context.coalesced = context.unknown = context.attempted = context.rejected = 2**64 - 1
    context.observe(None)
    for _ in range(2):
        context.observe({'status': 'dropped', 'reason': 'unknown', 'sequence': -1})
    assert context.take() is not None
    assert all(context.status()[key] == 2**64 - 1 for key in
               ['invalid_fields', 'coalesced', 'unknown_reasons', 'attempted', 'rejected_events'])
