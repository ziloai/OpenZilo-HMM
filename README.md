<p align="center">
  <a href="https://openzilo.com"><img src="docs/assets/openzilo-hero.png" alt="OpenZilo ring" width="960"></a>
</p>

<h1 align="center">OpenZilo HMM Gesture Recognition</h1>

<p align="center">Record ring gestures. Train your own models. Recognize them with Python.</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10+">
  <a href="https://github.com/ziloai/OpenZilo"><img src="https://img.shields.io/badge/SDK-OpenZilo%200.5.0-555555" alt="OpenZilo SDK 0.5.0"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MPL--2.0-555555" alt="MPL-2.0"></a>
</p>

<p align="center">
  <a href="https://openzilo.com">Website</a> ·
  <a href="https://openzilo.com/buy">Get the Kit</a> ·
  <a href="https://github.com/ziloai/OpenZilo">Python SDK</a> ·
  <a href="https://github.com/ziloai/OpenZilo-HMM/issues">Issues</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="README.zh-CN.md">简体中文</a>
</p>

An official OpenZilo reference project for training custom ring gestures with Hidden Markov Models (HMMs). It uses the [OpenZilo Python SDK](https://github.com/ziloai/OpenZilo) to collect six-axis IMU data over Bluetooth Low Energy, then trains and runs gesture classifiers on your computer.

OpenZilo is an open development platform for wearable interaction and **ComBodied AI**. Use this project as a starting point for gesture-driven agent commands, desktop shortcuts, robotics, and experimental interfaces.

> Training and recognition run on the **host computer**. This project does not flash firmware, upload models to the ring, or replace its built-in `0x0702` gesture recognition. It consumes raw `0x0605` IMU batches.

## What is included?

- BLE recording, CSV import, and terminal data entry.
- One left-to-right Gaussian HMM per gesture.
- Offline classification and continuous, motion-segmented BLE recognition.
- Six example datasets and pretrained models: finger snap, wrist flick, up, down, left, and right.
- Hardware-free tests for the SDK integration and offline pipeline.

```text
OpenZilo ring → OpenZilo SDK → IMU recordings → HMM training → Python recognition
                                   CSV / JSON ↗                  ↑
                                                     live BLE or recorded data
```

## Development kits and compatibility

Both **OpenZilo Q** (voice + gesture) and **OpenZilo X** (voice + gesture + physiological sensing) include an IMU. This project uses only the accelerometer and gyroscope; it does not use microphones or PPG.

The integration targets SDK **0.5.0**, protocol **v4**, and the upstream documentation's firmware baseline **`V2.000.0001.0015`**. Confirm IMU streaming support on your own kit and firmware; this is not a claim that every hardware revision has been tested.

## Quick start

### 1. Install

Use **Python 3.10+** (3.11 or 3.12 recommended) and Git. Clone the repository and install:

```bash
git clone https://github.com/ziloai/OpenZilo-HMM.git
cd OpenZilo-HMM
python -m venv .venv
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Requirements install NumPy, SciPy, hmmlearn, and the official OpenZilo SDK, which supplies Bleak. The SDK is pinned to upstream commit [`e9a861d`](https://github.com/ziloai/OpenZilo/commit/e9a861dd5c82a154fba0f1bafb628cd04fbd9555) rather than a moving branch. No local SDK copy or `sys.path` setup is needed.

Live use also needs a BLE adapter and Bluetooth permissions. Linux needs BlueZ; on macOS, allow Bluetooth access for the terminal or IDE. Offline training and recognition require no ring, Bluetooth connection, or `ffmpeg`.

### 2. Try it without hardware

```bash
# Classify five recorded repetitions with the included models.
python recognize.py --models pretrained_models --input "sample_data/向上.json"

# Train your own copies of all six example models.
python train_hmm.py --data sample_data --output models
python recognize.py --models models --input "sample_data/向左.json"
```

The included filenames and prediction labels are Chinese; see the table below. Testing on these same examples is a pipeline smoke test, **not an independent accuracy benchmark**.

> Models are Python pickle files. Load only files you trust: unpickling can execute code. If dependency changes make a model incompatible, retrain it from the JSON data.

### 3. Find your ring

```bash
python -m openzilo scan --timeout 10
python -m openzilo info --address "AA:BB:CC:DD:EE:FF"
```

Replace the example address in every command with the identifier returned by `scan`. macOS normally returns a UUID rather than a MAC address. Scanning may list other nearby BLE devices; select your ring. Disconnect other apps that are using it.

### 4. Record a gesture

The documented firmware starts in recording mode. **Before starting a live command**, switch the ring to gesture mode with a single button press if needed, and wait for the switch to finish. A single-press event alone does not confirm the resulting mode. The SDK cannot query or set that mode; `start_sensor_report()` must succeed before samples can be received.

```bash
python record_gesture.py --name snap --ring --address "AA:BB:CC:DD:EE:FF" --reps 5
```

1. Press **Enter in the terminal** to start a take.
2. Perform one gesture, then press Enter again to finish.
3. Repeat until five valid takes have been collected. Takes shorter than 12 samples are retried.
4. The dataset is saved to `gestures/snap.json`, with the sample rate reported by the device.

The receiver stays active between takes and discards idle samples. You do not need to hold the ring button during host-side recording or recognition. Avoid changing device modes mid-session.

Use at least two valid repetitions per gesture; five or more is a useful starting point. Record multiple gesture classes, keep ring placement and orientation consistent, and leave out unnecessarily long pauses. Recording again with the same name and output directory **overwrites** the JSON file. Cancelling before completion does not save a partial session.

Alternatively, import one repetition per **headerless CSV** file:

```bash
python record_gesture.py --name snap --from-csv snap1.csv snap2.csv --sample-rate 25
python record_gesture.py --name snap --interactive --reps 5 --sample-rate 25
```

### 5. Train and recognize

```bash
python train_hmm.py --data gestures --output models
python recognize.py --models models --ring --address "AA:BB:CC:DD:EE:FF"
```

Return to a still position between gestures so the motion segmenter can detect their boundaries. Press `Ctrl+C` to stop. The application attempts to stop IMU reporting before disconnecting. This does not switch the ring out of gesture mode.

## Example data and models

| Gesture | Dataset in `sample_data/` | Model in `pretrained_models/` |
| --- | --- | --- |
| Finger snap | `打响指-hmm.json` | `打响指-hmm.pkl` |
| Wrist flick | `甩-hmm.json` | `甩-hmm.pkl` |
| Up | `向上.json` | `向上.pkl` |
| Down | `向下.json` | `向下.pkl` |
| Left | `向左.json` | `向左.pkl` |
| Right | `向右.json` | `向右.pkl` |

Each dataset has five repetitions at **25 Hz**. These are development examples, not a general-purpose gesture dataset. Wearer, ring orientation, IMU range, sampling rate, and motion speed affect recognition. Retrain for your users and evaluate on separately recorded data.

## How it works

1. **Preprocessing:** a median filter followed by a second-order Butterworth low-pass filter.
2. **Features:** sliding-window mean, variance, RMS, and zero-crossing rate across six axes, producing 24 features per window.
3. **Training:** a Gaussian HMM with diagonal covariance per class. The left-to-right start/transition probabilities stay fixed; EM learns the means and covariances. Short sequences reduce the effective state count.
4. **Live segmentation:** movement relative to an adaptive baseline marks gesture boundaries, with pre-roll and cooldown. Offline mode classifies each recorded repetition directly, without this segmentation step.
5. **Classification:** choose the highest log-likelihood model. The displayed confidence is a score-gap heuristic, **not a calibrated probability**. With only one model it is fixed at `0.8`; there is no reliable unknown-gesture rejection by default.

### Parameters

| Parameter | Default | Used by |
| --- | --- | --- |
| `--sample-rate` | 25 Hz | Training and recognition; CSV/terminal recording metadata |
| `--cutoff-hz` | 10 Hz | Training and recognition; must be below half the sample rate |
| `--n-states` | 6 | Training; automatically reduced for short sequences |
| `--window-size` | 8 samples | Training and recognition |
| `--window-overlap` | 4 samples | Training and recognition |
| `energy_threshold` | 1500 raw-count distance | `MotionSegmenter` constructor in `recognize.py`, not a CLI flag |
| `min_gesture_len` / `max_gesture_len` | 10 / 125 samples | Segmenter; overly long motion is discarded |

**Use the same preprocessing parameters for training and recognition.** Existing `.pkl` files do not store preprocessing settings. The supplied models use the defaults above. Live recognition rejects a device rate different from `--sample-rate`; this option does not change the hardware sampling rate or resample data. JSON sample-rate metadata is checked during training and offline recognition. At the default window settings, at least 12 raw samples are needed to produce two feature windows.

## Data format

Each row contains six **raw signed 16-bit sensor values** in this order:

```text
ax, ay, az, gx, gy, gz
```

Do not substitute physical units such as g or degrees/second without retraining and adjusting segmentation thresholds. CSV files contain numeric rows only, no header or timestamp column. JSON uses this structure (shortened here; real repetitions need more samples):

```json
{
  "name": "snap",
  "created_at": "2026-09-29T12:00:00+00:00",
  "sample_rate_hz": 25,
  "num_repetitions": 2,
  "repetitions": [
    {"index": 0, "num_samples": 1, "data": [[1637, 530, -737, 1086, 476, 601]]},
    {"index": 1, "num_samples": 1, "data": [[1805, 22, -985, 647, 61, 245]]}
  ]
}
```

The recorder keeps the six axes and sample rate, not the SDK timestamps or batch sequence numbers. The legacy `threshold` field in the example JSON files is not used by this pipeline.

## Repository layout

```text
record_gesture.py       Record BLE data or import CSV/terminal data
train_hmm.py            Train one HMM per gesture
recognize.py            Offline and live host-side recognition
ring_stream.py          OpenZilo connection and IMU stream lifecycle
signal_filter.py        Median and low-pass filtering
feature_extractor.py    Sliding-window statistical features
sample_data/            Six example datasets
pretrained_models/      Six example pickle models
tests/                  Hardware-free regression tests
docs/sdk.md             SDK integration and migration notes (EN / 中文)
requirements.txt        Dependencies and pinned official SDK
```

## Troubleshooting

- **`No module named openzilo`:** install `requirements.txt` in the same Python environment used to run the scripts. Python below 3.10 is unsupported.
- **Device busy when starting IMU:** finish any active operation, check gesture mode and battery, then retry. Receiving a button event is not proof of a successful mode switch.
- **No IMU data or connection failure:** check Bluetooth permissions, the scanned address, other connected apps, and the device mode. Timeout warnings are shown; transport/protocol errors end the session rather than silently retrying forever.
- **No gesture or poor results:** check ring orientation and raw-value units, retrain on your own data, match preprocessing settings, and tune `MotionSegmenter` for your motions. Offline success does not guarantee live segmentation will work.

## Documentation and development

- [SDK migration and a minimal IMU example](docs/sdk.md)
- [OpenZilo SDK guide](https://github.com/ziloai/OpenZilo/blob/main/docs/python-sdk.zh-CN.md)
- [BLE protocol](https://github.com/ziloai/OpenZilo/blob/main/docs/protocol.zh-CN.md) and [architecture](https://github.com/ziloai/OpenZilo/blob/main/docs/architecture.zh-CN.md)
- [OpenZilo and ComBodied Agents research](https://github.com/ziloai/OpenZilo#research-and-partners)

Run tests without hardware:

```bash
python -m unittest discover -s tests -v
```

Report bugs and propose improvements through [GitHub Issues](https://github.com/ziloai/OpenZilo-HMM/issues). When contributing, describe the device/firmware and preprocessing settings needed to reproduce the issue. Do not include private device identifiers or recordings without permission. SDK integration tests use simulated devices; real BLE behavior still needs validation on the target kit.

## License

This project's software, documentation, example datasets, and pretrained models are licensed under [MPL-2.0](LICENSE), matching the OpenZilo SDK's software license. Brand and third-party marks belong to their respective owners; the license does not grant trademark rights.

See [NOTICE.md](NOTICE.md) for asset provenance and license scope.
