# Security Camera
Two small Windows programs that turn a webcam into a motion-detecting security camera that saves photos and video clips to a folder you choose.

| Program | What it saves |
|---|---|
| `security_camera_photos.py` | Photos and short video clips, only while there is motion. Shows a live picture. |
| `security_camera_with_audio.py` | One-minute MP4 clips. Clips without motion are deleted after 10 minutes. Saves a photo every second while there is motion. |

## Important: audio and privacy
- Both programs can record sound from the microphone. **Audio recording is OFF by default.**
- Recording people's voices without their consent is against the law in many places. For example, **Illinois requires the consent of everyone who is recorded.** Other states and countries have similar laws.
- Only tick **Record audio** if everyone who may be recorded knows and agrees. The program asks you to confirm before it turns audio on.
- Video recording of people can also be regulated where you live, for example in places where people expect privacy. Check your local rules, and tell people that a camera is on.
- While a program is running, a large indicator in the window shows what it is doing, for example **● RECORDING VIDEO** or **● RECORDING VIDEO + AUDIO** in red.

## What it does
- Watches a webcam and detects motion with OpenCV.
- Saves photos and video clips when something moves.
- Can also record sound, only if you turn it on.
- Keeps the folder from filling up: when the **Storage limit** is reached, the oldest recordings made by the program are deleted.
- `security_camera_with_audio.py` can test your camera (**Preview**) and microphone (**Test Mic**) and lets you choose the video resolution.

## What you need
- Windows 10 or 11 with a webcam. The programs also run on macOS and Linux, but they were made for Windows.
- Python 3.9 or newer.
- A microphone, only if you want to record sound.
- For `security_camera_with_audio.py`: [ffmpeg](https://ffmpeg.org) on your PATH to make MP4 files (on Windows: `winget install Gyan.FFmpeg`). Without ffmpeg, the clips are kept as `.avi` (and `.wav`) files.

## Install
1. Install Python from python.org. During setup, tick "Add Python to PATH".
2. Download this project and unzip it.
3. Open a command prompt in the project folder.
4. Run `pip install -r requirements.txt`
   - If PyAudio does not install, you can still use `security_camera_photos.py` without sound.
5. Optional: install ffmpeg for MP4 files.

## How to use

### security_camera_photos.py
1. Start the program: `py security_camera_photos.py`
2. Next to **Save Directory**, click **Browse** to choose where recordings go. The default is `SecurityRecordings` in your user folder.
3. Set **Photo Interval (sec)**, **Video Segment (sec)** and **Storage limit (GB, 0 = none)**.
4. Optional: tick **Always record (also save when there is no motion)**.
5. Optional: tick **Record audio** (read the privacy section above first).
6. Click **Start Recording**. The live picture appears under **Live Feed**. Saving starts when motion is detected.
7. Click **Stop Recording** to stop. Click **Open Save Folder** to see the files.

### security_camera_with_audio.py
1. Start the program: `py security_camera_with_audio.py`
2. Next to **Storage Folder**, click **Browse**. The default is `MotionCam` in your user folder.
3. Choose your **Camera** and **Resolution**. Click **Preview** to check the picture (press Q or ESC to close it).
4. Choose your **Microphone** and click **Test Mic** to see the **Mic Level**. The test does not save anything.
5. Move the **Motion sensitivity** slider. Lower numbers react to smaller movements.
6. Optional: tick **Record audio** (read the privacy section above first).
7. Set **Storage limit (GB, 0 = none)**.
8. Click **Start Recording**. Click **Stop** to stop. When you close the window, the program first finishes saving the last clip.

## Settings and options
- **Photo Interval (sec)**: time between photos while motion is seen.
- **Video Segment (sec)**: length of each video clip. After the last motion, recording goes on for one more clip length.
- **Min Area**: the smallest moving area (in pixels) that counts as motion.
- **Always record**: save photos and clips all the time, not only when there is motion. Off by default.
- **Record audio**: save sound with the clips. Off by default.
- **Storage limit (GB, 0 = none)**: the most space the program's own recordings may use in the folder. Default 10 GB (photos program) and 20 GB (audio program).

## Safety notes
- The programs save files only in the folder you choose.
- The storage limit deletes the **oldest** recordings made by the program when the folder is over the limit. A message in the status line tells you when this happens. Only files with the program's own names are deleted (`photo_...`, `video_...`, `audio_...` and `seg_...`, `captures/cap_...`). Your other files are never touched. Set the limit to 0 to turn this off.
- **Clear Recordings** (photos program) deletes only `photo_`, `video_` and `audio_` files made by the program. It shows how many files it will delete and asks first. This cannot be undone.
- `security_camera_with_audio.py` records all the time and deletes clips without motion after 10 minutes. Clips with motion are kept until the storage limit is reached.
- When ffmpeg cannot make an MP4, the raw `.avi` and `.wav` files are kept, so no recording is lost.
- Nothing is sent over the internet.

## Troubleshooting
- **"Could not open camera"** or **"ERROR: Cannot open camera"**: close other programs that use the camera (video calls), or choose another camera number.
- **"No Audio (PyAudio not installed)"**: run `pip install PyAudio`. On Windows you may need a recent Python version for a ready-made PyAudio package.
- **Clips are .avi files, not .mp4**: ffmpeg is not installed or not on your PATH. Install it and restart the program.
- **Too many or too few recordings**: change **Min Area** or **Motion sensitivity**.
- **"Some required Python packages are missing"**: run `pip install -r requirements.txt`.

## How it works
Both programs use OpenCV to read the webcam. `security_camera_photos.py` finds motion with a MOG2 background subtractor and contour areas, and writes XVID AVI clips, JPEG photos and optional WAV audio from PyAudio. `security_camera_with_audio.py` compares each frame with the one before it (frame differencing), records one-minute segments with optional audio from sounddevice, and merges them into H.264/AAC MP4 files with ffmpeg. On Windows it opens cameras with DirectShow (`cv2.CAP_DSHOW`, which exists only on Windows); on other systems it uses OpenCV's default backend. Camera, audio and file work run on background threads; the Tkinter window is updated only from the main thread through queues.

## License

MIT. See [LICENSE](LICENSE).
