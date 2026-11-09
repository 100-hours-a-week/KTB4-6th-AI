"""동시 회의 포화 탐색이 SLO를 넘긴 단계를 실패로 판정하는지 확인한다."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "capacity_sweep",
    Path(__file__).parents[1] / "scripts" / "modal_stt" / "capacity_sweep.py",
)
sweep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sweep)


def session(latencies, status="completed"):
    return {
        "status": status,
        "failed": 0,
        "segments": [
            {
                "endedAtSeconds": index * 30.0,
                "endToReceivedSeconds": latency,
                "queueSeconds": 0.5,
                "asrSeconds": 1.0,
                "embeddingTimedOut": False,
            }
            for index, latency in enumerate(latencies)
        ],
    }


STATS = {
    "failure": None,
    "wallSeconds": 100.0,
    "diar": [{"rpcSeconds": 0.05}],
    "diarCalls": [{"seconds": 0.06}],
    "batches": [{"size": 2, "seconds": 1.0}],
    "embeds": [{"seconds": 0.06}],
    "gpu": [{"usedMiB": 9000.0, "utilPercent": 60.0}],
}


def test_stage_fails_when_p95_exceeds_slo_or_meeting_fails():
    fast = [4.0] * 19 + [11.0]  # p95는 19번째 값 4초
    slow = [4.0] * 18 + [11.0] * 2  # p95가 11초

    passed = sweep.summarize(2, [session(fast), session(fast)], STATS)
    assert passed["passed"] and passed["endToReceivedP95Seconds"] == 4.0
    assert passed["windowP95Seconds"][0] == 4.0
    assert passed["diarBusyRatio"] == 0.05 / 100

    assert not sweep.summarize(2, [session(slow), session(slow)], STATS)["passed"]
    # 지연이 낮아도 회의 하나가 끝까지 처리되지 않으면 그 단계는 수용하지 못한 것이다.
    assert not sweep.summarize(2, [session(fast), session(fast, "failed")], STATS)["passed"]
