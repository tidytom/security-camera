import sys
import datetime
import os
import re
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import queue
import gc
import wave
from pathlib import Path

try:
    import cv2
    from PIL import Image, ImageTk
except ImportError as _missing:
    _message = (
        f"A required Python package is missing ({_missing.name}).\n\n"
        "Open a command prompt in this program's folder and run:\n"
        "  pip install -r requirements.txt\n\n"
        "Then start the program again."
    )
    print(_message, file=sys.stderr)
    try:
        _r = tk.Tk()
        _r.withdraw()
        messagebox.showerror("Missing packages", _message)
        _r.destroy()
    except Exception:
        pass
    sys.exit(1)

# PyAudio is optional. Without it the program records video and photos only.
try:
    import pyaudio
except ImportError:
    pyaudio = None

# Only files this program created match this pattern. "Clear Recordings" and the
# storage limit never touch any other file in the folder.
OWN_FILE_RE = re.compile(r"^(photo|video|audio)_\d{8}_\d{6}(_\d+)?\.(jpg|avi|wav)$", re.IGNORECASE)

AUDIO_CONSENT_TEXT = (
    "Recording people's voices without their consent is against the law in many places. "
    "For example, Illinois requires the consent of everyone who is recorded.\n\n"
    "Only turn on audio recording if everyone who may be recorded knows and agrees.\n\n"
    "Turn on audio recording?"
)


def own_recording_files(folder):
    """Return (path, size, mtime) for files this program created in folder."""
    result = []
    try:
        names = os.listdir(folder)
    except OSError:
        return result
    for name in names:
        if OWN_FILE_RE.match(name):
            path = os.path.join(folder, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if os.path.isfile(path):
                result.append((path, st.st_size, st.st_mtime))
    return result


def enforce_storage_limit(folder, limit_gb, exclude=()):
    """Delete the oldest of this program's own files until the total is under limit_gb.

    limit_gb <= 0 means no limit. Returns (files_deleted, bytes_freed).
    """
    if not limit_gb or limit_gb <= 0:
        return 0, 0
    limit = int(limit_gb * 1024 ** 3)
    files = sorted(own_recording_files(folder), key=lambda f: f[2])
    total = sum(f[1] for f in files)
    excluded = {os.path.normcase(os.path.abspath(p)) for p in exclude if p}
    deleted = 0
    freed = 0
    for path, size, _mtime in files:
        if total <= limit:
            break
        if os.path.normcase(os.path.abspath(path)) in excluded:
            continue
        try:
            os.remove(path)
        except OSError:
            continue
        total -= size
        freed += size
        deleted += 1
    return deleted, freed


def unique_path(folder, stem, ext):
    """folder/stem.ext, or folder/stem_1.ext, stem_2.ext ... if that name is taken."""
    path = os.path.join(folder, f"{stem}{ext}")
    counter = 1
    while os.path.exists(path):
        path = os.path.join(folder, f"{stem}_{counter}{ext}")
        counter += 1
    return path


class HomeSecuritySystem:
    def __init__(self, root):
        self.root = root
        self.root.title("Home Security Motion Detection System")
        self.root.geometry("900x760")

        # Initialize variables
        self.cap = None
        self.audio = None
        self.is_recording = False
        self.motion_detected = False
        self.motion_count = 0

        # Recording settings
        self.save_directory = str(Path.home() / "SecurityRecordings")
        self.photo_interval = 5  # seconds
        self.video_segment_length = 5  # seconds
        self.fps = 30
        self.storage_limit_gb = 10.0  # 0 = no limit

        # Motion detection settings
        self.motion_threshold = 25
        self.min_area = 500
        self.background_subtractor = None

        # Audio settings
        self.audio_chunk = 1024
        self.audio_format = pyaudio.paInt16 if pyaudio else None
        self.audio_channels = 1
        self.audio_rate = 44100

        # Threading and queues. The processing thread and the screen each get their
        # own copy of every frame, so the saved video gets all frames.
        self.frame_queue = queue.Queue(maxsize=30)
        self.display_queue = queue.Queue(maxsize=2)
        self.event_queue = queue.Queue()
        self.audio_lock = threading.Lock()
        self.camera_thread = None
        self.processing_thread = None
        self.audio_thread = None

        # Timing variables
        self.last_photo_time = 0
        self.last_video_time = 0
        self.last_motion_time = 0
        self.current_video_writer = None
        self.current_video_path = None
        self.current_segment_stamp = None
        self.segment_active = False
        self.current_audio_frames = []
        self.record_audio_enabled = False
        self.always_record_enabled = False

        # Background subtraction for motion detection
        self.background_subtractor = cv2.createBackgroundSubtractorMOG2(detectShadows=True)

        # Create save directory
        os.makedirs(self.save_directory, exist_ok=True)

        # Initialize PyAudio first (before GUI setup)
        self.audio_available = False
        if pyaudio is not None:
            try:
                self.audio = pyaudio.PyAudio()
                self.audio_available = True
            except Exception:
                self.audio = None

        # Setup GUI (now that audio_available is defined)
        self.setup_gui()

        self.status_var.set("Ready - Select directory and start recording")
        self.root.after(33, self.gui_tick)

    def setup_gui(self):
        # Main container
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # File path selection frame
        path_frame = ttk.LabelFrame(main_frame, text="Recording Settings", padding="10")
        path_frame.pack(fill=tk.X, pady=5)

        # Directory selection
        dir_frame = ttk.Frame(path_frame)
        dir_frame.pack(fill=tk.X, pady=2)

        ttk.Label(dir_frame, text="Save Directory:").pack(side=tk.LEFT)
        self.dir_var = tk.StringVar(value=self.save_directory)
        dir_entry = ttk.Entry(dir_frame, textvariable=self.dir_var, width=50)
        dir_entry.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

        ttk.Button(dir_frame, text="Browse", command=self.browse_directory).pack(side=tk.RIGHT, padx=5)

        # Interval settings
        interval_frame = ttk.Frame(path_frame)
        interval_frame.pack(fill=tk.X, pady=5)

        ttk.Label(interval_frame, text="Photo Interval (sec):").pack(side=tk.LEFT)
        self.photo_interval_var = tk.IntVar(value=self.photo_interval)
        photo_spin = ttk.Spinbox(interval_frame, from_=1, to=60, textvariable=self.photo_interval_var, width=10)
        photo_spin.pack(side=tk.LEFT, padx=5)

        ttk.Label(interval_frame, text="Video Segment (sec):").pack(side=tk.LEFT, padx=(20, 5))
        self.video_interval_var = tk.IntVar(value=self.video_segment_length)
        video_spin = ttk.Spinbox(interval_frame, from_=5, to=300, textvariable=self.video_interval_var, width=10)
        video_spin.pack(side=tk.LEFT, padx=5)

        ttk.Label(interval_frame, text="Storage limit (GB, 0 = none):").pack(side=tk.LEFT, padx=(20, 5))
        self.storage_limit_var = tk.DoubleVar(value=self.storage_limit_gb)
        limit_spin = ttk.Spinbox(interval_frame, from_=0, to=10000, increment=1,
                                 textvariable=self.storage_limit_var, width=8)
        limit_spin.pack(side=tk.LEFT, padx=5)

        ttk.Label(path_frame, text="When the folder is over the storage limit, the oldest photo_/video_/audio_ "
                                   "files made by this program are deleted.",
                  font=("Arial", 8)).pack(anchor="w")

        # Recording options
        options_frame = ttk.Frame(path_frame)
        options_frame.pack(fill=tk.X, pady=5)

        self.always_record_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(options_frame, text="Always record (also save when there is no motion)",
                        variable=self.always_record_var).pack(side=tk.LEFT)

        self.record_audio_var = tk.BooleanVar(value=False)
        self.audio_check = ttk.Checkbutton(options_frame, text="Record audio",
                                           variable=self.record_audio_var,
                                           command=self.on_record_audio_toggled)
        self.audio_check.pack(side=tk.LEFT, padx=(20, 5))
        if not self.audio_available:
            self.audio_check.config(state=tk.DISABLED)

        # Motion detection settings
        motion_frame = ttk.LabelFrame(main_frame, text="Motion Detection", padding="10")
        motion_frame.pack(fill=tk.X, pady=5)

        # Sensitivity slider
        sensitivity_frame = ttk.Frame(motion_frame)
        sensitivity_frame.pack(fill=tk.X, pady=2)

        ttk.Label(sensitivity_frame, text="Sensitivity:").pack(side=tk.LEFT)
        self.sensitivity_var = tk.IntVar(value=self.motion_threshold)
        sensitivity_scale = ttk.Scale(sensitivity_frame, from_=10, to=100,
                                      orient=tk.HORIZONTAL, variable=self.sensitivity_var,
                                      length=200, command=self.update_sensitivity)
        sensitivity_scale.pack(side=tk.LEFT, padx=10)

        ttk.Label(sensitivity_frame, text="Min Area:").pack(side=tk.LEFT, padx=(20, 5))
        self.min_area_var = tk.IntVar(value=self.min_area)
        area_spin = ttk.Spinbox(sensitivity_frame, from_=100, to=5000, textvariable=self.min_area_var, width=10)
        area_spin.pack(side=tk.LEFT, padx=5)

        # Video display
        video_frame = ttk.LabelFrame(main_frame, text="Live Feed", padding="5")
        video_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        self.video_label = ttk.Label(video_frame)
        self.video_label.pack(fill=tk.BOTH, expand=True)

        # Status and controls
        control_frame = ttk.Frame(main_frame)
        control_frame.pack(fill=tk.X, pady=10)

        # Recording indicator (always visible)
        self.rec_indicator = tk.Label(control_frame, text="Not recording", font=("Arial", 11, "bold"), fg="gray")
        self.rec_indicator.pack(anchor="w")

        # Status display
        status_frame = ttk.Frame(control_frame)
        status_frame.pack(fill=tk.X, pady=2)

        self.status_var = tk.StringVar(value="Not recording")
        status_label = ttk.Label(status_frame, textvariable=self.status_var, font=("Arial", 10))
        status_label.pack(side=tk.LEFT)

        # Motion indicator
        self.motion_var = tk.StringVar(value="No Motion")
        motion_label = ttk.Label(status_frame, textvariable=self.motion_var,
                                font=("Arial", 10, "bold"), foreground="green")
        motion_label.pack(side=tk.LEFT, padx=20)

        # Counters
        self.motion_count_var = tk.StringVar(value="Motion Events: 0")
        count_label = ttk.Label(status_frame, textvariable=self.motion_count_var, font=("Arial", 10))
        count_label.pack(side=tk.LEFT, padx=20)

        # Control buttons
        button_frame = ttk.Frame(control_frame)
        button_frame.pack(fill=tk.X, pady=5)

        self.record_button = ttk.Button(button_frame, text="Start Recording",
                                       command=self.toggle_recording)
        self.record_button.pack(side=tk.LEFT, padx=5)

        ttk.Button(button_frame, text="Open Save Folder",
                  command=self.open_save_folder).pack(side=tk.LEFT, padx=5)

        ttk.Button(button_frame, text="Clear Recordings",
                  command=self.clear_recordings).pack(side=tk.LEFT, padx=5)

        # Audio indicator
        if self.audio_available:
            self.audio_var = tk.StringVar(value="Audio off")
        else:
            self.audio_var = tk.StringVar(value="No Audio (PyAudio not installed)")

        audio_label = ttk.Label(button_frame, textvariable=self.audio_var, font=("Arial", 9))
        audio_label.pack(side=tk.RIGHT, padx=10)

    def on_record_audio_toggled(self):
        if self.record_audio_var.get():
            if not messagebox.askyesno("Record audio?", AUDIO_CONSENT_TEXT, icon="warning"):
                self.record_audio_var.set(False)
        if self.audio_available:
            self.audio_var.set("Audio on" if self.record_audio_var.get() else "Audio off")

    def browse_directory(self):
        directory = filedialog.askdirectory(initialdir=self.save_directory)
        if directory:
            self.save_directory = directory
            self.dir_var.set(directory)
            os.makedirs(directory, exist_ok=True)

    def update_sensitivity(self, *args):
        self.motion_threshold = self.sensitivity_var.get()

    def toggle_recording(self):
        if not self.is_recording:
            self.start_recording()
        else:
            self.stop_recording()

    def start_recording(self):
        if self.is_recording:
            return

        # Update settings from GUI
        self.save_directory = self.dir_var.get()
        self.photo_interval = self.photo_interval_var.get()
        self.video_segment_length = self.video_interval_var.get()
        self.min_area = self.min_area_var.get()
        try:
            self.storage_limit_gb = max(0.0, float(self.storage_limit_var.get()))
        except (tk.TclError, ValueError):
            self.storage_limit_gb = 0.0
        self.record_audio_enabled = bool(self.record_audio_var.get()) and self.audio_available
        self.always_record_enabled = bool(self.always_record_var.get())

        # Create directory if it doesn't exist
        os.makedirs(self.save_directory, exist_ok=True)

        # Initialize camera
        self.cap = cv2.VideoCapture(0)
        if not self.cap.isOpened():
            messagebox.showerror("Error", "Could not open camera")
            return

        # Set camera properties
        self.cap.set(cv2.CAP_PROP_FPS, self.fps)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

        self.is_recording = True
        self.record_button.config(text="Stop Recording")
        self.status_var.set("Recording - Monitoring for motion...")

        # Reset counters
        self.motion_count = 0
        self.last_photo_time = 0
        self.last_video_time = 0
        self.last_motion_time = 0
        self.segment_active = False
        with self.audio_lock:
            self.current_audio_frames = []

        # Start threads
        self.camera_thread = threading.Thread(target=self.camera_loop, daemon=True)
        self.processing_thread = threading.Thread(target=self.processing_loop, daemon=True)

        if self.record_audio_enabled:
            self.audio_thread = threading.Thread(target=self.audio_loop, daemon=True)
            self.audio_thread.start()
        else:
            self.audio_thread = None

        self.camera_thread.start()
        self.processing_thread.start()

    def stop_recording(self):
        self.is_recording = False
        self.record_button.config(text="Start Recording")
        self.status_var.set("Stopped")

        # Wait for the worker threads before releasing the camera and the video file
        for t in (self.camera_thread, self.processing_thread, self.audio_thread):
            if t is not None and t.is_alive() and t is not threading.current_thread():
                t.join(timeout=3)
        self.camera_thread = self.processing_thread = self.audio_thread = None

        # Finish the open video segment and its audio
        self.finalize_video_segment()

        # Clean up camera
        if self.cap is not None:
            if self.cap.isOpened():
                self.cap.release()
            self.cap = None

        # Empty the frame queues for the next start
        for q in (self.frame_queue, self.display_queue):
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break

        gc.collect()

    @staticmethod
    def _put_latest(q, item):
        """Put item in q; if q is full, drop the oldest item first."""
        try:
            q.put(item, block=False)
        except queue.Full:
            try:
                q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put(item, block=False)
            except queue.Full:
                pass

    def camera_loop(self):
        while self.is_recording:
            ret, frame = self.cap.read()
            if not ret:
                self.event_queue.put(("error", "Camera disconnected"))
                break

            # Add timestamp to frame
            timestamp = datetime.datetime.now()
            cv2.putText(frame, timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                       (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

            # One copy for processing/saving, one for the screen
            self._put_latest(self.frame_queue, (frame, timestamp))
            self._put_latest(self.display_queue, frame.copy())

            time.sleep(1/self.fps)

    def audio_loop(self):
        if not self.audio_available:
            return

        try:
            stream = self.audio.open(format=self.audio_format,
                                   channels=self.audio_channels,
                                   rate=self.audio_rate,
                                   input=True,
                                   frames_per_buffer=self.audio_chunk)

            while self.is_recording:
                try:
                    data = stream.read(self.audio_chunk, exception_on_overflow=False)
                    # Sound is kept only while a video segment is being saved
                    with self.audio_lock:
                        if self.segment_active:
                            self.current_audio_frames.append(data)
                except Exception as e:
                    print(f"Audio error: {e}")
                    time.sleep(0.01)

            stream.stop_stream()
            stream.close()

        except Exception as e:
            print(f"Audio setup error: {e}")
            self.event_queue.put(("log", f"Audio error: {e}"))

    def processing_loop(self):
        while self.is_recording:
            try:
                frame, timestamp = self.frame_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            # Motion detection
            motion_detected = self.detect_motion(frame)

            current_time = time.time()
            if motion_detected:
                self.last_motion_time = current_time

            # Save only while there is motion (and for one segment length after it),
            # unless "Always record" is on.
            saving = self.always_record_enabled or (
                self.last_motion_time and current_time - self.last_motion_time < self.video_segment_length
            )

            if saving:
                # Take photo every interval
                if current_time - self.last_photo_time >= self.photo_interval:
                    self.save_photo(frame, timestamp)
                    self.last_photo_time = current_time

                # Handle video segments
                if self.current_video_writer is None:
                    self.start_new_video_segment(frame)
                    self.last_video_time = current_time
                elif current_time - self.last_video_time >= self.video_segment_length:
                    self.finalize_video_segment()
                    self.start_new_video_segment(frame)
                    self.last_video_time = current_time

                # Write frame to current video
                if self.current_video_writer:
                    self.current_video_writer.write(frame)
            elif self.current_video_writer is not None:
                self.finalize_video_segment()

            # Update motion status
            if motion_detected != self.motion_detected:
                self.motion_detected = motion_detected
                if motion_detected:
                    self.motion_count += 1
                    self.event_queue.put(("motion", True))
                else:
                    self.event_queue.put(("motion", False))

    def detect_motion(self, frame):
        # Convert to grayscale
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        # Apply background subtraction
        fg_mask = self.background_subtractor.apply(gray)

        # Noise reduction
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_CLOSE, kernel)
        fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_OPEN, kernel)

        # Find contours
        contours, _ = cv2.findContours(fg_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        # Check for significant motion
        for contour in contours:
            area = cv2.contourArea(contour)
            if area > self.min_area:
                return True

        return False

    def save_photo(self, frame, timestamp):
        filepath = unique_path(self.save_directory, f"photo_{timestamp.strftime('%Y%m%d_%H%M%S')}", ".jpg")
        if cv2.imwrite(filepath, frame):
            self.event_queue.put(("log", f"Photo saved: {os.path.basename(filepath)}"))
            self.apply_storage_limit()
        else:
            self.event_queue.put(("log", f"Could not save photo: {os.path.basename(filepath)}"))

    def start_new_video_segment(self, frame):
        timestamp = datetime.datetime.now()
        self.current_segment_stamp = timestamp.strftime('%Y%m%d_%H%M%S')
        filepath = unique_path(self.save_directory, f"video_{self.current_segment_stamp}", ".avi")

        height, width = frame.shape[:2]
        fourcc = cv2.VideoWriter_fourcc(*'XVID')
        self.current_video_writer = cv2.VideoWriter(filepath, fourcc, self.fps, (width, height))
        self.current_video_path = filepath
        with self.audio_lock:
            self.current_audio_frames = []
            self.segment_active = True

        self.event_queue.put(("log", f"Started video: {os.path.basename(filepath)}"))

    def finalize_video_segment(self):
        with self.audio_lock:
            self.segment_active = False
        if self.current_video_writer:
            self.current_video_writer.release()
            self.current_video_writer = None

        # Save audio segment
        if self.record_audio_enabled:
            self.save_audio_segment()

        if self.current_video_path:
            self.current_video_path = None
            self.apply_storage_limit()

    def save_audio_segment(self):
        # Take the collected sound under the lock so the audio thread cannot add to it meanwhile
        with self.audio_lock:
            frames = self.current_audio_frames
            self.current_audio_frames = []
        if not frames:
            return

        stamp = self.current_segment_stamp or datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        filepath = unique_path(self.save_directory, f"audio_{stamp}", ".wav")

        try:
            wf = wave.open(filepath, 'wb')
            wf.setnchannels(self.audio_channels)
            wf.setsampwidth(self.audio.get_sample_size(self.audio_format))
            wf.setframerate(self.audio_rate)
            wf.writeframes(b''.join(frames))
            wf.close()

            self.event_queue.put(("log", f"Audio saved: {os.path.basename(filepath)}"))
        except Exception as e:
            print(f"Error saving audio: {e}")

    def apply_storage_limit(self):
        deleted, freed = enforce_storage_limit(self.save_directory, self.storage_limit_gb,
                                               exclude=[self.current_video_path])
        if deleted:
            self.event_queue.put(("log", f"Storage limit {self.storage_limit_gb:g} GB reached: deleted "
                                         f"{deleted} oldest recording file(s), {freed / 1024 ** 2:.0f} MB"))

    def gui_tick(self):
        """Runs on the Tk main thread about 30 times per second."""
        # Process events
        try:
            while True:
                event_type, event_data = self.event_queue.get_nowait()

                if event_type == "motion":
                    self.update_motion_status(event_data)
                elif event_type == "error":
                    self.show_error(event_data)
                elif event_type == "log":
                    self.update_status_briefly(event_data)
        except queue.Empty:
            pass

        # Update video display with the newest frame
        frame = None
        try:
            while True:
                frame = self.display_queue.get_nowait()
        except queue.Empty:
            pass
        if frame is not None and self.is_recording:
            # Convert to RGB
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

            # Resize for display
            h, w = rgb_frame.shape[:2]
            max_height = 400
            if h > max_height:
                scale = max_height / h
                rgb_frame = cv2.resize(rgb_frame, (int(w * scale), max_height))

            # Convert to PhotoImage (on the main thread)
            imgtk = ImageTk.PhotoImage(image=Image.fromarray(rgb_frame))
            self.update_video_display(imgtk)

        self.update_recording_indicator()
        self.root.after(33, self.gui_tick)  # ~30 FPS

    def update_recording_indicator(self):
        if not self.is_recording:
            text, color = "Not recording", "gray"
        elif self.segment_active:
            text = "● RECORDING VIDEO + AUDIO" if self.record_audio_enabled else "● RECORDING VIDEO"
            color = "red"
        elif self.record_audio_enabled:
            text, color = "Camera and microphone on - waiting for motion", "dark orange"
        else:
            text, color = "Camera on - waiting for motion", "dark orange"
        if self.rec_indicator.cget("text") != text:
            self.rec_indicator.config(text=text, fg=color)

    def update_video_display(self, imgtk):
        self.video_label.imgtk = imgtk
        self.video_label.config(image=imgtk)

    def update_motion_status(self, motion_detected):
        if motion_detected:
            self.motion_var.set("🔴 MOTION DETECTED")
        else:
            self.motion_var.set("✅ No Motion")

        self.motion_count_var.set(f"Motion Events: {self.motion_count}")

    def update_status_briefly(self, message):
        self.status_var.set(message)

        def reset_status():
            if self.is_recording:
                self.status_var.set("Recording - Monitoring for motion...")

        self.root.after(2000, reset_status)

    def show_error(self, message):
        self.stop_recording()
        self.status_var.set(f"Error: {message}")

    def open_save_folder(self):
        if os.path.exists(self.save_directory):
            os.startfile(self.save_directory)  # Windows
            # For macOS: os.system(f"open '{self.save_directory}'")
            # For Linux: os.system(f"xdg-open '{self.save_directory}'")

    def clear_recordings(self):
        if self.is_recording:
            messagebox.showinfo("Clear Recordings", "Stop recording first.")
            return
        folder = self.dir_var.get() or self.save_directory
        files = own_recording_files(folder)
        if not files:
            messagebox.showinfo("Clear Recordings", f"No recordings made by this program were found in:\n{folder}")
            return
        total_mb = sum(f[1] for f in files) / 1024 ** 2
        if messagebox.askyesno("Clear Recordings",
                               f"Delete {len(files)} recording file(s) ({total_mb:.0f} MB) from:\n{folder}\n\n"
                               "Only photo_, video_ and audio_ files made by this program are deleted. "
                               "Other files in the folder are not touched.\n\n"
                               "This cannot be undone. Continue?", icon="warning"):
            errors = 0
            for path, _size, _mtime in files:
                try:
                    os.remove(path)
                except OSError:
                    errors += 1
            if errors:
                messagebox.showerror("Error", f"{errors} file(s) could not be deleted.")
            else:
                messagebox.showinfo("Success", "Recordings cleared successfully")

    def __del__(self):
        if self.audio_available and self.audio:
            self.audio.terminate()


def main():
    root = tk.Tk()

    app = HomeSecuritySystem(root)

    # Handle window closing
    def on_closing():
        if app.is_recording:
            app.stop_recording()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_closing)
    root.mainloop()


if __name__ == "__main__":
    main()
