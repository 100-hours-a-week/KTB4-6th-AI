"""회의 하나의 화자별 근거를 누적해 등록 참가자와의 부분 1:1 연결을 계산한다."""

import math
from collections.abc import Sequence

from meety_ai.stt.schemas import EMBEDDING_DIM

# shortcut: 같은 회의 탐색에서 고른 실험 재현값이며, 독립 회의로 검증하면 재조정한다.
MIN_SECONDS = 5.0
THRESHOLD = 0.40


def _unit(vector: Sequence[float] | None) -> list[float] | None:
    if vector is None or len(vector) != EMBEDDING_DIM:
        return None
    norm = math.sqrt(math.fsum(value * value for value in vector))
    if not math.isfinite(norm) or norm <= 0:
        return None
    return [value / norm for value in vector]


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    return math.fsum(a * b for a, b in zip(left, right, strict=True))


class SpeakerMapper:
    """구간 생성 순서대로 observe()를 호출해야 하며, 이후 구간을 앞선 판정에 쓰지 않는다."""

    def __init__(self, profiles: dict[int, Sequence[float]]):
        self.profiles = {member: _unit(vector) for member, vector in profiles.items()}
        self.sums: dict[int, list[float]] = {}
        self.seconds: dict[int, float] = {}
        self.mapping: dict[int, int] = {}
        self.discovered: set[int] = set()
        self.sent: list[tuple[int, int | None]] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.profiles)

    def observe(
        self, slot: int, vector: Sequence[float] | None, seconds: float
    ) -> tuple[int | None, list[tuple[int, int | None]] | None]:
        """전사에 넣을 화자 ID와, 바뀐 경우에만 발견한 화자 전체의 매핑 스냅샷을 반환한다.

        비교할 등록 벡터가 없으면 모든 연결이 null이므로 기존처럼 익명 화자만 전달한다.
        """
        if not self.enabled:
            return slot, None
        self.discovered.add(slot)
        speaker = slot
        vector = _unit(vector)
        if vector is None:
            # 추출 실패 구간은 누적하지 않고 화자를 보류한다.
            speaker = None
        else:
            total = self.sums.setdefault(slot, [0.0] * EMBEDDING_DIM)
            for index, value in enumerate(vector):
                total[index] += seconds * value
            self.seconds[slot] = self.seconds.get(slot, 0.0) + seconds
            self.mapping = self._assign()
            mapped = self.mapping.get(slot)
            best = max(self.profiles, key=lambda member: _dot(self.profiles[member], vector))
            if mapped is not None and best != mapped:
                # 구간의 최고점 참가자가 slot 연결과 다르면 이 전사만 보류한다.
                speaker = None
        snapshot = [(item, self.mapping.get(item)) for item in sorted(self.discovered)]
        if snapshot == self.sent:
            return speaker, None
        self.sent = snapshot
        return speaker, snapshot

    def _assign(self) -> dict[int, int]:
        """THRESHOLD 미만 연결 없이 (cosine - THRESHOLD) 합이 최대인 부분 1:1 연결을 구한다."""
        slots = [slot for slot in sorted(self.sums) if self.seconds[slot] >= MIN_SECONDS]
        features = [_unit(self.sums[slot]) for slot in slots]
        # 참가자를 하나씩 배정하며 사용한 slot 집합별 최고 점수만 남긴다. slot은 최대 8개다.
        best: dict[int, tuple[float, dict[int, int]]] = {0: (0.0, {})}
        for member, profile in self.profiles.items():
            gains = [_dot(profile, feature) - THRESHOLD for feature in features]
            following = dict(best)
            for used, (score, mapping) in best.items():
                for index, gain in enumerate(gains):
                    if used >> index & 1 or gain < 0:
                        continue
                    key, value = used | 1 << index, score + gain
                    if key not in following or value > following[key][0]:
                        following[key] = (value, {**mapping, slots[index]: member})
            best = following
        return max(best.values(), key=lambda item: item[0])[1]
