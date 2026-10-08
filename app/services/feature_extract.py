"""音频特征提取（复现边缘端 newSys.py，**16kHz、去环境维**）。

特征顺序 = [MFCC(40), 频谱对比度(7), STFT-mel-DCT(12), ZCR(1)] = 60 维。
（边缘端原实现为 62 维，末尾另有 温湿度 2 维；本模块统一去掉环境维。）
"""
from typing import Union

import numpy as np
from python_speech_features import mfcc
from python_speech_features.sigproc import framesig, powspec
from scipy import signal
from scipy.fftpack import dct

FEATURE_DIM = 60
DEFAULT_SAMPLE_RATE = 16000


def extract_spectral_contrast(
    sig: np.ndarray,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    winlen: float = 0.025,
    winstep: float = 0.010,
    num_bands: int = 7,
    threshold_percentile: float = 15,
) -> np.ndarray:
    frames = framesig(sig, winlen * sample_rate, winstep * sample_rate)
    power_spectrum = powspec(frames, 1024)
    nfft = int(power_spectrum.shape[1] * 2 - 2)
    freqs = np.fft.rfftfreq(nfft, 1.0 / sample_rate)

    band_edges = np.logspace(np.log10(1), np.log10(sample_rate / 2), num_bands + 1)
    contrast_features = []
    for frame_power in power_spectrum:
        magnitude = np.sqrt(frame_power).astype(np.float64)
        magnitude[0] = 0
        frame_contrast = []
        for i in range(num_bands):
            idx = np.where((freqs >= band_edges[i]) & (freqs < band_edges[i + 1]))[0]
            if len(idx) == 0:
                frame_contrast.append(0.0)
                continue
            band_mags = magnitude[idx]
            peak = float(np.max(band_mags))
            sorted_mags = np.sort(band_mags)
            threshold_count = int(len(sorted_mags) * (threshold_percentile / 100.0)) or 1
            valley = float(np.mean(sorted_mags[:threshold_count]))
            if valley < 1e-10:
                valley = 1e-10
            if peak < 1e-10:
                peak = 1e-10
            frame_contrast.append(20 * np.log10(peak / valley))
        contrast_features.append(frame_contrast)
    return np.array(contrast_features)


def stft_feature(audio: np.ndarray, sr: int) -> np.ndarray:
    n_fft = 2048
    hop_length = 512
    window = signal.windows.hann(n_fft)
    n_frames = 1 + (len(audio) - n_fft) // hop_length
    if n_frames < 1:
        return np.zeros((1, 12))
    zxx = np.zeros((n_fft // 2 + 1, n_frames), dtype=np.complex64)
    for i in range(n_frames):
        start = i * hop_length
        frame = audio[start:start + n_fft] * window
        zxx[:, i] = np.fft.rfft(frame, n=n_fft)

    magnitude = np.abs(zxx)
    n_mels = 12
    half_n_fft = n_fft // 2
    mel_max = 2595 * np.log10(1 + (sr / 2) / 700)
    mel_points = np.linspace(0, mel_max, n_mels + 2)
    hz_points = 700 * (10 ** (mel_points / 2595) - 1)
    bins = np.floor((half_n_fft + 1) * hz_points / sr).astype(int)
    bins = np.clip(bins, 0, half_n_fft)
    fbank = np.zeros((n_mels, half_n_fft + 1))
    for m in range(1, n_mels + 1):
        f_m_minus, f_m, f_m_plus = bins[m - 1], bins[m], bins[m + 1]
        if f_m - f_m_minus > 0:
            fbank[m - 1, f_m_minus:f_m] = (np.arange(f_m_minus, f_m) - f_m_minus) / (f_m - f_m_minus)
        if f_m_plus - f_m > 0:
            fbank[m - 1, f_m:f_m_plus] = (f_m_plus - np.arange(f_m, f_m_plus)) / (f_m_plus - f_m)
    filter_banks = np.dot(fbank, magnitude)
    filter_banks = np.where(filter_banks == 0, np.finfo(float).eps, filter_banks)
    filter_banks = 20 * np.log10(filter_banks)
    mf = dct(filter_banks, type=2, axis=1, norm='ortho')[:, :12]
    return np.mean(mf, axis=0).reshape(1, 12)


def extract_60d_feature(
    audio: Union[np.ndarray, list, bytes],
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> np.ndarray:
    """输入 int16 PCM（ndarray / bytes），返回 60 维 float64 特征。"""
    if isinstance(audio, bytes):
        arr = np.frombuffer(audio, dtype=np.int16)
    else:
        arr = np.asarray(audio)
    y = arr.astype(np.float32) / 32768.0

    mf = np.mean(
        mfcc(y, sample_rate, winlen=0.025, winstep=0.01, numcep=40, nfilt=40, nfft=512),
        axis=0,
    ).astype(np.float64)
    zcr = float(np.mean(np.abs(np.diff(np.sign(y))) / 2))
    contrast = np.mean(extract_spectral_contrast(y, sample_rate), axis=0).astype(np.float64)
    stft = np.mean(stft_feature(y, sample_rate), axis=0).astype(np.float64)

    feature = np.hstack([mf, contrast, stft, np.array([zcr])]).astype(np.float64)
    return feature
