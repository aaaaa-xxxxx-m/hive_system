"""把目录下的 WAV 负样本转换为 60 维特征(.npy) 并上传到 MinIO。

用法（在 backend 容器内执行）:
    python /app/scripts/convert_negatives.py /tmp/neg
上传目标前缀: datasets/preset/negative/
"""
import io
import os
import sys

import numpy as np
from scipy.io import wavfile
from scipy.signal import resample_poly

# 让脚本可 import app.*
sys.path.insert(0, "/app")

from app.services.feature_extract import extract_60d_feature  # noqa: E402
from app.services.minio_client import get_minio_client  # noqa: E402

TARGET_RATE = 16000
TARGET_PREFIX = "datasets/preset/negative"


def load_wav_as_int16_mono_16k(path: str) -> np.ndarray:
    sr, data = wavfile.read(path)
    if data.dtype != np.int16:
        if np.issubdtype(data.dtype, np.floating):
            data = np.clip(data, -1.0, 1.0)
            data = (data * 32767).astype(np.int16)
        else:
            data = data.astype(np.int16)
    if data.ndim > 1:
        data = data.mean(axis=1).astype(np.int16)
    if sr != TARGET_RATE:
        g = int(np.gcd(sr, TARGET_RATE))
        data = resample_poly(data.astype(np.float32), TARGET_RATE // g, sr // g).astype(np.int16)
    return data


def main():
    directory = sys.argv[1] if len(sys.argv) > 1 else "/tmp/neg"
    files = sorted(f for f in os.listdir(directory) if f.lower().endswith(".wav"))
    if not files:
        print(f"no wav in {directory}")
        sys.exit(1)

    minio = get_minio_client()
    ok = 0
    for name in files:
        path = os.path.join(directory, name)
        try:
            audio = load_wav_as_int16_mono_16k(path)
            feat = extract_60d_feature(audio, TARGET_RATE).astype(np.float32)
            buf = io.BytesIO()
            np.save(buf, feat)
            key = f"{TARGET_PREFIX}/{os.path.splitext(name)[0]}.npy"
            minio.upload_file(key, buf.getvalue(), content_type="application/octet-stream")
            ok += 1
            if ok % 50 == 0:
                print(f"uploaded {ok}/{len(files)}")
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {name}: {e}")
    print(f"DONE uploaded={ok}/{len(files)} dim={len(extract_60d_feature(load_wav_as_int16_mono_16k(os.path.join(directory, files[0])), TARGET_RATE))}")


if __name__ == "__main__":
    main()
