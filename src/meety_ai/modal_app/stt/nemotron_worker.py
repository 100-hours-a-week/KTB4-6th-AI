"""별도 Transformers 환경에서 세션별 Nemotron 캐시를 유지한다."""

import base64
import contextlib
import importlib.metadata
import json
import os
import sys
from pathlib import Path


def main(output):
    import numpy as np
    import torch
    from huggingface_hub import snapshot_download
    from transformers import AutoModelForAudioFrameClassification, AutoProcessor

    model_id = "nvidia/Nemotron-3-Diarization"
    revision = os.environ["NEMOTRON_REVISION"]
    snapshot = snapshot_download(
        model_id, revision=revision, allow_patterns=["*.json", "*.safetensors"]
    )
    processor = AutoProcessor.from_pretrained(snapshot)
    processor.set_streaming_mode("low_latency")
    model = AutoModelForAudioFrameClassification.from_pretrained(snapshot).to("cuda").eval()
    feature = processor.feature_extractor
    geometry = {
        "first_samples": processor.num_samples_first_audio_chunk,
        "chunk_samples": processor.num_samples_per_audio_chunk,
        "mel_step": processor.num_mel_frames_per_step,
        "frame_samples": feature.hop_length,
        "latency_ms": processor.streaming_latency_ms,
    }
    if feature.sampling_rate != 16000 or geometry["frame_samples"] != 160:
        raise RuntimeError("Nemotron 음성·프레임 규격이 설계와 다릅니다.")
    print(
        json.dumps(
            {
                "ready": True,
                "geometry": geometry,
                "revision": Path(snapshot).name,
                "transformers": importlib.metadata.version("transformers"),
            }
        ),
        file=output,
        flush=True,
    )
    caches = {}
    for line in sys.stdin:
        request = json.loads(line)
        key = request["session"]
        if request["op"] == "release":
            caches.pop(key, None)
            response = {"released": True}
        else:
            pcm = base64.b64decode(request["pcm"], validate=True)
            audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
            if not request["first"] and key not in caches:
                raise RuntimeError("연속 세션의 화자 캐시가 없습니다.")
            # 매우 짧은 EOF도 STFT 창을 만들되 실제 음성 밖의 프레임은 호출자가 제외한다.
            if request["last"] and len(audio) < feature.n_fft:
                audio = np.pad(audio, (0, feature.n_fft - len(audio)))
            with torch.inference_mode():
                inputs = processor(
                    audio,
                    sampling_rate=16000,
                    is_streaming=True,
                    is_first_audio_chunk=request["first"],
                    is_last_audio_chunk=request["last"],
                ).to(model.device, dtype=model.dtype)
                result = model(**inputs, speaker_cache=caches.get(key))
                if request["last"]:
                    caches.pop(key, None)
                elif result.speaker_cache is None:
                    raise RuntimeError("Nemotron 중간 청크의 캐시가 없습니다.")
                else:
                    caches[key] = result.speaker_cache
                probabilities = result.logits[0].sigmoid()
                confidence, speaker = probabilities.max(dim=-1)
                active = torch.zeros_like(probabilities, dtype=torch.uint8)
                active.scatter_(1, speaker[:, None], (confidence > 0.5).to(torch.uint8)[:, None])
                response = {
                    "active": active.cpu().tolist(),
                    "next_start": processor.audio_chunk_start(
                        request["mel_cursor"] + geometry["mel_step"]
                    ),
                }
            del result, inputs
        print(json.dumps(response), file=output, flush=True)


if __name__ == "__main__":
    output = sys.stdout
    with contextlib.redirect_stdout(sys.stderr):
        main(output)
