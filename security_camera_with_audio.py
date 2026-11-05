import os
import re
import sys
import queue
import subprocess
import shutil
import time
import wave
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from datetime import datetime
from collections import deque
from pathlib import Path

# (pip package, import name)
REQUIRED = [
    ("opencv-python", "cv2"),
    ("numpy", "numpy"),
    ("sounddevice", "sounddevice"),
]


def check_dependencies():
    """Show a clear message and stop if a required package is missing. Nothing is installed automatically."""
    missing = []
    for pkg, mod in REQUIRED:
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
        except OSError:
            # sounddevice raises OSError when the PortAudio library is missing
            missing.append(pkg)
    if not missing:
        return
    message = (
        "Some required Python packages are missing:\n  " + ", ".join(missing) + "\n\n"
        "Open a command prompt in this program's folder and run:\n"
        "  pip install -r requirements.txt\n\n"
        "Then start the program again."
    )
    print(message, file=sys.stderr)
    try:
        _r = tk.Tk()
        _r.withdraw()
        messagebox.showerror("Missing packages", message)
        _r.destroy()
    except Exception:
        pass
    sys.exit(1)


check_dependencies()

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import sounddevice as sd  # noqa: E402

FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None

# cv2.CAP_DSHOW (DirectShow) only exists on Windows. Other systems use the default backend.
CAMERA_BACKEND = cv2.CAP_DSHOW if sys.platform.startswith("win") else cv2.CAP_ANY

# Only files this program created match these patterns. The storage limit never
# touches any other file in the folder.
OWN_SEGMENT_RE = re.compile(r"^seg_\d{8}_\d{6}(_\d+)?\.(mp4|avi|wav)$", re.IGNORECASE)
OWN_CAPTURE_RE = re.compile(r"^cap_\d{8}_\d{6}_\d{6}\.jpg$", re.IGNORECASE)

AUDIO_CONSENT_TEXT = (
    "Recording people's voices without their consent is against the law in many places. "
    "For example, Illinois requires the consent of everyone who is recorded.\n\n"
    "Only turn on audio recording if everyone who may be recorded knows and agrees.\n\n"
    "Turn on audio recording?"
)


def own_recording_files(storage_dir):
    """Return (path, size, mtime) for files this program created."""
    result = []
    folders = [(storage_dir, OWN_SEGMENT_RE), (os.path.join(storage_dir, "captures"), OWN_CAPTURE_RE)]
    for folder, pattern in folders:
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            if pattern.match(name):
                path = os.path.join(folder, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if os.path.isfile(path):
                    result.append((path, st.st_size, st.st_mtime))
    return result


def enforce_storage_limit(storage_dir, limit_gb):
    """Delete the oldest of this program's own files until the total is under limit_gb.

    limit_gb <= 0 means no limit. Returns (deleted_paths, bytes_freed).
    """
    if not limit_gb or limit_gb <= 0:
        return [], 0
    limit = int(limit_gb * 1024 ** 3)
    files = sorted(own_recording_files(storage_dir), key=lambda f: f[2])
    total = sum(f[1] for f in files)
    deleted = []
    freed = 0
    for path, size, _mtime in files:
        if total <= limit:
            break
        try:
            os.remove(path)
        except OSError:
            continue
        total -= size
        freed += size
        deleted.append(path)
    return deleted, freed


RESOLUTIONS = {
    "1920x1080 (16:9 Full HD)": (1920, 1080),
    "1280x720 (16:9 HD)": (1280, 720),
    "1280x960 (4:3)": (1280, 960),
    "1024x768 (4:3)": (1024, 768),
    "960x540 (16:9)": (960, 540),
    "800x600 (4:3)": (800, 600),
    "640x480 (4:3)": (640, 480),
    "640x360 (16:9)": (640, 360),
}


class MotionCam:
    def __init__(self):
        self.running = False
        self.storage_dir = ""
        self.cam_index = 0
        self.audio_device = None
        self.resolution = (1920, 1080)
        self.segment_duration = 60
        self.discard_interval = 600
        self.cooldown = 1800
        self.min_contour_area = 500
        self.screenshot_interval = 1
        self.record_audio_enabled = False  # off unless the user turns it on
        self.storage_limit_gb = 20.0  # 0 = no limit

        self.segments = deque()
        self.last_motion_time = None
        self.in_motion_event = False
        self.recording_segment = False
        self.lock = threading.Lock()
        # Messages for the window. Worker threads put (kind, text); the Tk thread reads them.
        self.ui_queue = queue.Queue()

    def post(self, kind, text):
        self.ui_queue.put((kind, text))

    def detect_cameras(self):
        cams = []
        for i in range(10):
            cap = cv2.VideoCapture(i, CAMERA_BACKEND)
            if cap.isOpened():
                cams.append(i)
                cap.release()
        return cams

    def detect_audio_devices(self):
        devices = []
        for i, d in enumerate(sd.query_devices()):
            if d['max_input_channels'] > 0:
                devices.append((i, d['name']))
        return devices

    def test_mic(self, device_index, level_callback, duration=5):
        samplerate = 44100
        chunk = 1024
        try:
            def audio_callback(indata, frames, time_info, status):
                rms = np.sqrt(np.mean(indata ** 2))
                db = max(0, min(100, int(20 * np.log10(rms + 1e-10) + 80)))
                level_callback(db)

            with sd.InputStream(device=device_index, channels=1,
                                samplerate=samplerate, blocksize=chunk,
                                callback=audio_callback):
                time.sleep(duration)
            level_callback(-1)  # signal done
        except Exception as e:
            print(f"Mic test error: {e}")
            level_callback(-1)

    def apply_resolution(self, cap):
        w, h = self.resolution
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)

    def preview_camera(self, cam_index):
        cap = cv2.VideoCapture(cam_index, CAMERA_BACKEND)
        if not cap.isOpened():
            return
        self.apply_resolution(cap)
        cv2.namedWindow("Camera Preview - press Q or ESC to close", cv2.WINDOW_NORMAL)
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            cv2.imshow("Camera Preview - press Q or ESC to close", frame)
            key = cv2.waitKey(30) & 0xFF
            if key == ord('q') or key == 27:
                break
            if cv2.getWindowProperty("Camera Preview - press Q or ESC to close", cv2.WND_PROP_VISIBLE) < 1:
                break
        cap.release()
        cv2.destroyAllWindows()

    def record_audio(self, filepath, stop_event, samplerate=44100):
        """Record from the microphone until stop_event is set (the video segment ended or Stop was pressed)."""
        chunks = []
        try:
            def callback(indata, frames, time_info, status):
                chunks.append(indata.copy())

            with sd.InputStream(samplerate=samplerate, channels=1, dtype='int16',
                                device=self.audio_device, callback=callback):
                while not stop_event.wait(0.1):
                    pass
            if chunks:
                with wave.open(filepath, 'w') as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(samplerate)
                    wf.writeframes(np.concatenate(chunks).tobytes())
        except Exception as e:
            print(f"Audio error: {e}")
            self.post("log", f"Audio error: {e}")

    def merge_segment(self, video_path, audio_path, final_path):
        """Convert the AVI (and WAV) to one MP4. Raw files are removed only if the MP4 was really made."""
        if not FFMPEG_AVAILABLE:
            return video_path
        has_audio = audio_path and os.path.exists(audio_path) and os.path.getsize(audio_path) > 44
        cmd = ["ffmpeg", "-y", "-i", video_path]
        if has_audio:
            cmd += ["-i", audio_path]
        cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "23"]
        if has_audio:
            cmd += ["-c:a", "aac", "-b:a", "128k", "-shortest"]
        cmd.append(final_path)
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=120)
        except Exception as e:
            print(f"Merge error: {e}")
            return video_path
        if result.returncode == 0 and os.path.exists(final_path) and os.path.getsize(final_path) > 0:
            for raw in (video_path, audio_path):
                if raw and os.path.exists(raw):
                    try:
                        os.remove(raw)
                    except OSError:
                        pass
            return final_path
        print(f"ffmpeg failed (exit code {result.returncode}) - keeping raw AVI/WAV files")
        self.post("log", "ffmpeg could not make the MP4 - kept the raw AVI/WAV files")
        if os.path.exists(final_path):
            try:
                os.remove(final_path)  # remove a broken, partly written MP4
            except OSError:
                pass
        return video_path

    def record_segment(self, cap):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        # Never overwrite an earlier segment: add _1, _2 ... if this name is taken
        stem = f"seg_{ts}"
        counter = 1
        while any(os.path.exists(os.path.join(self.storage_dir, stem + ext)) for ext in (".avi", ".wav", ".mp4")):
            stem = f"seg_{ts}_{counter}"
            counter += 1
        video_path = os.path.join(self.storage_dir, f"{stem}.avi")
        audio_path = os.path.join(self.storage_dir, f"{stem}.wav")
        final_path = os.path.join(self.storage_dir, f"{stem}.mp4")
        captures_dir = os.path.join(self.storage_dir, "captures")
        os.makedirs(captures_dir, exist_ok=True)

        fps = 15.0
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fourcc = cv2.VideoWriter_fourcc(*'XVID')
        writer = cv2.VideoWriter(video_path, fourcc, fps, (w, h))

        audio_thread = None
        audio_stop = threading.Event()
        if self.record_audio_enabled and self.audio_device is not None:
            audio_thread = threading.Thread(
                target=self.record_audio,
                args=(audio_path, audio_stop),
                daemon=True,
            )
            audio_thread.start()
        self.recording_segment = True

        motion_detected = False
        prev_gray = None
        start = time.time()
        frame_count = 0
        last_screenshot = 0

        while self.running and (time.time() - start) < self.segment_duration:
            ret, frame = cap.read()
            if not ret:
                break

            writer.write(frame)
            frame_count += 1
            now = time.time()
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (21, 21), 0)

            if prev_gray is not None:
                delta = cv2.absdiff(prev_gray, gray)
                thresh = cv2.threshold(delta, 25, 255, cv2.THRESH_BINARY)[1]
                thresh = cv2.dilate(thresh, None, iterations=2)
                contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

                frame_has_motion = False
                for c in contours:
                    if cv2.contourArea(c) > self.min_contour_area:
                        frame_has_motion = True
                        break

                if frame_has_motion:
                    motion_detected = True
                    with self.lock:
                        self.last_motion_time = datetime.now()

                    if (now - last_screenshot) >= self.screenshot_interval:
                        last_screenshot = now
                        ss_path = os.path.join(
                            captures_dir,
                            f"cap_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jpg"
                        )
                        cv2.imwrite(ss_path, frame)

            prev_gray = gray

            target_delay = (1.0 / fps) - (time.time() - now)
            if target_delay > 0:
                cv2.waitKey(max(1, int(target_delay * 1000)))
            else:
                cv2.waitKey(1)

        writer.release()
        audio_stop.set()
        if audio_thread is not None:
            audio_thread.join(timeout=10)
        self.recording_segment = False

        final_path = self.merge_segment(video_path, audio_path if audio_thread else None, final_path)

        return final_path, motion_detected, datetime.now()

    def cleanup_old_segments(self):
        with self.lock:
            if self.in_motion_event:
                return

            now = datetime.now()
            to_remove = []
            for seg in self.segments:
                path, has_motion, ts = seg
                age = (now - ts).total_seconds()
                if age > self.discard_interval and not has_motion:
                    to_remove.append(seg)

            for seg in to_remove:
                self.segments.remove(seg)
                try:
                    os.remove(seg[0])
                    print(f"Discarded: {os.path.basename(seg[0])}")
                except OSError:
                    pass
                # A segment kept as raw AVI may also have a WAV beside it
                raw_wav = os.path.splitext(seg[0])[0] + ".wav"
                if seg[0].lower().endswith(".avi") and os.path.exists(raw_wav):
                    try:
                        os.remove(raw_wav)
                    except OSError:
                        pass

    def apply_storage_limit(self):
        """Delete the oldest own files (also motion segments) when over the storage limit."""
        deleted, freed = enforce_storage_limit(self.storage_dir, self.storage_limit_gb)
        if not deleted:
            return
        gone = {os.path.normcase(p) for p in deleted}
        with self.lock:
            for seg in list(self.segments):
                if os.path.normcase(seg[0]) in gone:
                    self.segments.remove(seg)
        msg = (f"Storage limit {self.storage_limit_gb:g} GB reached: deleted {len(deleted)} oldest "
               f"file(s), {freed / 1024 ** 2:.0f} MB")
        print(msg)
        self.post("log", msg)

    def run_loop(self):
        try:
            self._run_loop()
        finally:
            self.running = False
            self.recording_segment = False
            self.post("stopped", "Stopped")

    def _run_loop(self):
        cap = cv2.VideoCapture(self.cam_index, CAMERA_BACKEND)
        if not cap.isOpened():
            self.post("status", "ERROR: Cannot open camera")
            return

        self.apply_resolution(cap)
        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        print(f"Recording at {actual_w}x{actual_h}")
        self.post("status", f"Recording at {actual_w}x{actual_h}...")

        while self.running:
            path, has_motion, ts = self.record_segment(cap)
            self.segments.append((path, has_motion, ts))

            with self.lock:
                if has_motion:
                    if not self.in_motion_event:
                        self.in_motion_event = True
                        print(f"Motion event started at {ts.strftime('%H:%M:%S')}")
                        self.post("status", f"MOTION DETECTED - {ts.strftime('%H:%M:%S')}")

                if self.in_motion_event and self.last_motion_time:
                    elapsed = (datetime.now() - self.last_motion_time).total_seconds()
                    if elapsed >= self.cooldown:
                        self.in_motion_event = False
                        print(f"Motion event ended - no motion for {self.cooldown}s")
                        self.post("status", "Recording... (motion event ended)")

            self.cleanup_old_segments()
            self.apply_storage_limit()
            self.post("count", f"Segments kept: {len(self.segments)}")

        cap.release()


def main():
    cam = MotionCam()

    root = tk.Tk()
    root.title("Motion Detection Camera")
    root.geometry("500x680")
    root.resizable(False, False)

    frame = ttk.Frame(root, padding=15)
    frame.pack(fill=tk.BOTH, expand=True)

    # Storage folder
    ttk.Label(frame, text="Storage Folder:").grid(row=0, column=0, sticky="w", pady=5)
    folder_var = tk.StringVar(value=str(Path.home() / "MotionCam"))
    folder_entry = ttk.Entry(frame, textvariable=folder_var, width=30)
    folder_entry.grid(row=0, column=1, pady=5)

    def browse():
        d = filedialog.askdirectory()
        if d:
            folder_var.set(d)

    ttk.Button(frame, text="Browse", command=browse).grid(row=0, column=2, padx=5, pady=5)

    # Camera selection
    ttk.Label(frame, text="Camera:").grid(row=1, column=0, sticky="w", pady=5)
    cams = cam.detect_cameras()
    cam_var = tk.StringVar(value=str(cams[0]) if cams else "0")
    cam_combo = ttk.Combobox(frame, textvariable=cam_var, values=[str(c) for c in cams], width=27, state="readonly")
    cam_combo.grid(row=1, column=1, sticky="w", pady=5)

    def preview_cam():
        idx = int(cam_var.get())
        cam.resolution = RESOLUTIONS[res_var.get()]
        threading.Thread(target=cam.preview_camera, args=(idx,), daemon=True).start()

    ttk.Button(frame, text="Preview", command=preview_cam).grid(row=1, column=2, padx=5, pady=5)

    # Resolution selection
    ttk.Label(frame, text="Resolution:").grid(row=2, column=0, sticky="w", pady=5)
    res_options = list(RESOLUTIONS.keys())
    res_var = tk.StringVar(value=res_options[0])
    res_combo = ttk.Combobox(frame, textvariable=res_var, values=res_options, width=27, state="readonly")
    res_combo.grid(row=2, column=1, columnspan=2, sticky="w", pady=5)

    # Audio device selection
    ttk.Label(frame, text="Microphone:").grid(row=3, column=0, sticky="w", pady=5)
    audio_devs = cam.detect_audio_devices()
    audio_names = [f"{i}: {n}" for i, n in audio_devs]
    audio_var = tk.StringVar(value=audio_names[0] if audio_names else "None")
    audio_combo = ttk.Combobox(frame, textvariable=audio_var, values=audio_names, width=27, state="readonly")
    audio_combo.grid(row=3, column=1, sticky="w", pady=5)

    ttk.Button(frame, text="Test Mic", command=lambda: start_mic_test()).grid(row=3, column=2, padx=5, pady=5)

    # Mic level meter
    mic_frame = ttk.LabelFrame(frame, text="Mic Level", padding=5)
    mic_frame.grid(row=4, column=0, columnspan=3, sticky="ew", pady=5)
    mic_progress = ttk.Progressbar(mic_frame, orient="horizontal", length=400, mode="determinate", maximum=100)
    mic_progress.pack(fill="x")
    mic_label = ttk.Label(mic_frame, text="Click 'Test Mic' to check your microphone")
    mic_label.pack()

    def start_mic_test():
        sel = audio_var.get()
        if not sel or sel == "None":
            mic_label.config(text="No microphone selected")
            return
        dev_idx = int(sel.split(":")[0])
        mic_label.config(text="Listening for 5 seconds... speak now!")
        mic_progress["value"] = 0

        def on_level(db):
            # Called from the audio thread: hand the value to the Tk thread through the queue
            cam.post("mic", db)

        threading.Thread(target=cam.test_mic, args=(dev_idx, on_level), daemon=True).start()

    ttk.Separator(frame, orient="horizontal").grid(row=5, column=0, columnspan=3, sticky="ew", pady=10)

    # Settings
    ttk.Label(frame, text="Motion sensitivity (lower = more sensitive):").grid(row=6, column=0, columnspan=2, sticky="w")
    sens_var = tk.IntVar(value=500)
    sens_scale = ttk.Scale(frame, from_=100, to=5000, variable=sens_var, orient="horizontal")
    sens_scale.grid(row=6, column=2, sticky="ew")

    # Audio recording (off by default) and storage limit
    record_audio_var = tk.BooleanVar(value=False)

    def on_record_audio_toggled():
        if record_audio_var.get() and not messagebox.askyesno("Record audio?", AUDIO_CONSENT_TEXT, icon="warning"):
            record_audio_var.set(False)

    ttk.Checkbutton(frame, text="Record audio", variable=record_audio_var,
                    command=on_record_audio_toggled).grid(row=7, column=0, sticky="w", pady=5)
    ttk.Label(frame, text="Storage limit (GB, 0 = none):").grid(row=7, column=1, sticky="e", pady=5)
    limit_var = tk.DoubleVar(value=cam.storage_limit_gb)
    ttk.Spinbox(frame, from_=0, to=10000, increment=1, textvariable=limit_var, width=8).grid(row=7, column=2, sticky="w", pady=5)
    ttk.Label(frame, text="Over the limit, the oldest seg_ and cap_ files made by this program are deleted.",
              font=("Segoe UI", 8)).grid(row=8, column=0, columnspan=3, sticky="w")

    ttk.Separator(frame, orient="horizontal").grid(row=9, column=0, columnspan=3, sticky="ew", pady=10)

    # Recording indicator (always visible)
    rec_indicator = tk.Label(frame, text="Not recording", font=("Segoe UI", 12, "bold"), fg="gray")
    rec_indicator.grid(row=10, column=0, columnspan=3, pady=2)

    # Status
    status_var = tk.StringVar(value="Ready" if FFMPEG_AVAILABLE else
                              "Ready (ffmpeg not found: segments are kept as .avi/.wav)")
    ttk.Label(frame, textvariable=status_var, font=("Segoe UI", 11, "bold")).grid(row=11, column=0, columnspan=3, pady=5)

    count_var = tk.StringVar(value="Segments kept: 0")
    ttk.Label(frame, textvariable=count_var).grid(row=12, column=0, columnspan=3)

    rec_thread = None
    closing = False

    def set_idle_buttons():
        start_btn.config(state="normal")
        stop_btn.config(state="disabled")
        preview_btn.config(state="normal")
        mic_test_btn.config(state="normal")

    def start():
        nonlocal rec_thread
        if rec_thread is not None and rec_thread.is_alive():
            return
        cam.storage_dir = folder_var.get()
        os.makedirs(cam.storage_dir, exist_ok=True)

        cam.cam_index = int(cam_var.get())
        cam.resolution = RESOLUTIONS[res_var.get()]
        cam.min_contour_area = sens_var.get()
        cam.record_audio_enabled = record_audio_var.get()
        try:
            cam.storage_limit_gb = max(0.0, float(limit_var.get()))
        except (tk.TclError, ValueError):
            cam.storage_limit_gb = 0.0

        sel = audio_var.get()
        cam.audio_device = int(sel.split(":")[0]) if sel and sel != "None" else None

        cam.running = True
        rec_thread = threading.Thread(target=cam.run_loop, daemon=True)
        rec_thread.start()
        start_btn.config(state="disabled")
        stop_btn.config(state="normal")
        preview_btn.config(state="disabled")
        mic_test_btn.config(state="disabled")

    def stop():
        cam.running = False
        if rec_thread is not None and rec_thread.is_alive():
            status_var.set("Stopping... saving the last segment")
            start_btn.config(state="disabled")
            stop_btn.config(state="disabled")
        else:
            set_idle_buttons()

    def poll_ui():
        """Runs on the Tk thread: apply messages from the worker threads."""
        try:
            while True:
                kind, value = cam.ui_queue.get_nowait()
                if kind == "status":
                    status_var.set(value)
                elif kind == "count":
                    count_var.set(value)
                elif kind == "log":
                    status_var.set(value)
                elif kind == "stopped":
                    status_var.set(value)
                    set_idle_buttons()
                elif kind == "mic":
                    if value == -1:
                        mic_label.config(text="Mic test complete")
                    else:
                        mic_progress.config(value=value)
        except queue.Empty:
            pass

        if cam.running and cam.recording_segment:
            text = "● RECORDING VIDEO + AUDIO" if cam.record_audio_enabled and cam.audio_device is not None else "● RECORDING VIDEO"
            color = "red"
        elif cam.running:
            text, color = "Starting camera...", "dark orange"
        else:
            text, color = "Not recording", "gray"
        if rec_indicator.cget("text") != text:
            rec_indicator.config(text=text, fg=color)

        if closing and (rec_thread is None or not rec_thread.is_alive()):
            root.destroy()
            return
        root.after(100, poll_ui)

    def on_close():
        nonlocal closing
        # Stop, then wait (without freezing the window) until the last segment is saved
        closing = True
        stop()

    btn_frame = ttk.Frame(frame)
    btn_frame.grid(row=13, column=0, columnspan=3, pady=15)
    start_btn = ttk.Button(btn_frame, text="Start Recording", command=start)
    start_btn.pack(side="left", padx=10)
    stop_btn = ttk.Button(btn_frame, text="Stop", command=stop, state="disabled")
    stop_btn.pack(side="left", padx=10)

    # Keep references to buttons we disable during recording
    preview_btn = frame.grid_slaves(row=1, column=2)[0]
    mic_test_btn = frame.grid_slaves(row=3, column=2)[0]

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.after(100, poll_ui)
    root.mainloop()


if __name__ == "__main__":
    main()
