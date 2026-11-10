import os
import sys
import threading
import time
import subprocess
import shutil
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

import cv2
import numpy as np
import mss
import soundfile as sf
import soundcard as sc

# --- глобальные горячие клавиши ---
try:
    import keyboard as _kb
    HAS_KB = True
except ImportError:
    HAS_KB = False


# DPI-совместимость (до создания Tk)
if os.name == "nt":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def resource_path(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def find_ffmpeg():
    candidates = []
    if hasattr(sys, "_MEIPASS"):
        candidates.append(os.path.join(sys._MEIPASS, "ffmpeg.exe"))
    if getattr(sys, "frozen", False):
        candidates.append(os.path.join(os.path.dirname(sys.executable), "ffmpeg.exe"))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg.exe"))
    for path in candidates:
        if os.path.exists(path) and os.path.getsize(path) > 100000:
            return path
    return shutil.which("ffmpeg")


class ScreenRecorder:
    def __init__(self, fps=20, record_audio=True, record_mic=True,
                 samplerate=48000, region=None):
        self.fps = fps
        self.record_audio = record_audio
        self.record_mic = record_mic
        self.samplerate = samplerate
        self.region = region
        self.recording = False
        self.paused = False
        self.video_writer = None
        self.video_thread = None
        self.audio_thread = None
        self.mic_thread = None
        self.video_path = None
        self.audio_path = None
        self.final_path = None
        self._audio_frames = []
        self._mic_frames = []
        self.audio_ok = False
        self.mic_ok = False
        self.warning = None

    # ---------- запуск ----------
    def start(self, filename):
        if self.recording:
            return
        self.final_path = filename
        base, _ = os.path.splitext(filename)
        self.video_path = base + ".video.tmp.mp4"
        self.audio_path = base + ".audio.tmp.wav"
        self._audio_frames = []
        self._mic_frames = []
        self.audio_ok = False
        self.mic_ok = False
        self.warning = None

        self.recording = True
        self.paused = False

        self.video_thread = threading.Thread(target=self._record_video, daemon=True)
        self.video_thread.start()

        if self.record_audio:
            self.audio_thread = threading.Thread(
                target=self._record_system_audio, daemon=True)
            self.audio_thread.start()

        if self.record_mic:
            self.mic_thread = threading.Thread(
                target=self._record_mic, daemon=True)
            self.mic_thread.start()

    # ---------- видео ----------
    def _record_video(self):
        try:
            with mss.mss() as sct:
                if self.region:
                    monitor = {
                        "left": int(self.region["left"]),
                        "top": int(self.region["top"]),
                        "width": int(self.region["width"]),
                        "height": int(self.region["height"]),
                    }
                else:
                    monitor = sct.monitors[1]

                width = monitor["width"] - (monitor["width"] % 2)
                height = monitor["height"] - (monitor["height"] % 2)

                fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                self.video_writer = cv2.VideoWriter(
                    self.video_path, fourcc, self.fps, (width, height)
                )

                start_time = time.time()
                total_pause = 0.0
                pause_start = None
                frame_count = 0
                last_frame = None

                while self.recording:
                    if self.paused:
                        if pause_start is None:
                            pause_start = time.time()
                        time.sleep(0.03)
                        continue
                    if pause_start is not None:
                        total_pause += time.time() - pause_start
                        pause_start = None

                    active = time.time() - start_time - total_pause
                    target = int(active * self.fps) + 1

                    if frame_count < target:
                        try:
                            img = np.array(sct.grab(monitor))
                            frame = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
                            if frame.shape[1] != width or frame.shape[0] != height:
                                frame = frame[:height, :width]
                            last_frame = frame
                        except Exception:
                            pass

                        while frame_count < target and self.recording:
                            if last_frame is not None:
                                self.video_writer.write(last_frame)
                            frame_count += 1
                    else:
                        time.sleep(0.002)

                if self.video_writer is not None:
                    self.video_writer.release()
                    self.video_writer = None
        except Exception as e:
            print("Ошибка записи видео:", e)

    # ---------- системный звук ----------
    def _record_system_audio(self):
        try:
            speaker = sc.default_speaker()
            mic = sc.get_microphone(speaker.name, include_loopback=True)
            if mic is None:
                print("Loopback-устройство не найдено")
                return
            block = 1024
            with mic.recorder(samplerate=self.samplerate, channels=2) as rec:
                self.audio_ok = True
                while self.recording:
                    data = rec.record(numframes=block)
                    if self.paused:
                        continue
                    self._audio_frames.append(data)
        except Exception as e:
            print("Ошибка записи системного звука:", e)
            self.audio_ok = False

    # ---------- микрофон ----------
    def _record_mic(self):
        try:
            mic = sc.default_microphone()
            if mic is None:
                print("Микрофон не найден")
                return
        except Exception as e:
            print("Ошибка доступа к микрофону:", e)
            return

        block = 1024
        for ch in (1, 2):
            try:
                with mic.recorder(samplerate=self.samplerate, channels=ch) as rec:
                    self.mic_ok = True
                    while self.recording:
                        data = rec.record(numframes=block)
                        if self.paused:
                            continue
                        self._mic_frames.append(data)
                    return
            except Exception as e:
                print(f"Микрофон (канал {ch}) недоступен: {e}")
                self._mic_frames = []
                self.mic_ok = False

    # ---------- управление ----------
    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def stop(self):
        self.recording = False
        if self.video_thread:
            self.video_thread.join(timeout=15)
        if self.audio_thread:
            self.audio_thread.join(timeout=15)
        if self.mic_thread:
            self.mic_thread.join(timeout=15)

        mixed = self._mix_audio()
        if mixed is not None and len(mixed) > 0:
            try:
                sf.write(self.audio_path, mixed, self.samplerate)
            except Exception as e:
                print("Ошибка сохранения аудио:", e)
                try:
                    os.remove(self.audio_path)
                except OSError:
                    pass
                self.audio_path = None
        else:
            self.audio_path = None

        self._merge()

    def _mix_audio(self):
        sys_audio = None
        mic_audio = None
        if self._audio_frames:
            try:
                sys_audio = np.concatenate(self._audio_frames, axis=0)
            except Exception as e:
                print("concat sys error:", e)
        if self._mic_frames:
            try:
                mic_audio = np.concatenate(self._mic_frames, axis=0)
            except Exception as e:
                print("concat mic error:", e)

        def to_stereo(a):
            if a is None:
                return None
            a = a.astype(np.float32, copy=False)
            if a.ndim == 1:
                return np.stack([a, a], axis=-1)
            if a.shape[1] == 1:
                return np.repeat(a, 2, axis=1)
            if a.shape[1] > 2:
                return a[:, :2]
            return a

        sys_audio = to_stereo(sys_audio)
        mic_audio = to_stereo(mic_audio)

        if sys_audio is None:
            return mic_audio
        if mic_audio is None:
            return sys_audio

        n = max(len(sys_audio), len(mic_audio))
        out = np.zeros((n, 2), dtype=np.float32)
        out[:len(sys_audio)] += sys_audio
        out[:len(mic_audio)] += mic_audio

        peak = float(np.max(np.abs(out))) if out.size else 0.0
        if peak > 0.99:
            out *= (0.99 / peak)
        return out

    # ---------- склейка ----------
    def _merge(self):
        if not os.path.exists(self.video_path):
            return

        has_audio = bool(
            self.audio_path
            and os.path.exists(self.audio_path)
            and os.path.getsize(self.audio_path) > 1000
        )

        ffmpeg = find_ffmpeg()

        if not ffmpeg:
            try:
                os.replace(self.video_path, self.final_path)
            except Exception as e:
                print("rename error:", e)
            if has_audio:
                wav_final = os.path.splitext(self.final_path)[0] + ".wav"
                try:
                    os.replace(self.audio_path, wav_final)
                except Exception:
                    pass
                self.warning = (
                    "ffmpeg не найден",
                    "Видео сохранено без звука.\n"
                    "Аудио сохранено отдельным WAV-файлом.\n\n"
                    "Убедитесь, что ffmpeg.exe лежит в одной папке с программой!",
                )
            return

        common_v = [
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "26",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
        ]

        if has_audio:
            cmd = [
                ffmpeg, "-y",
                "-i", self.video_path,
                "-i", self.audio_path,
                *common_v,
                "-c:a", "aac",
                "-b:a", "128k",
                "-shortest",
                self.final_path,
            ]
        else:
            cmd = [
                ffmpeg, "-y",
                "-i", self.video_path,
                *common_v,
                self.final_path,
            ]

        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            subprocess.run(cmd, check=True, creationflags=flags)
            try:
                os.remove(self.video_path)
            except OSError:
                pass
            if has_audio:
                try:
                    os.remove(self.audio_path)
                except OSError:
                    pass
        except Exception as e:
            print("Ошибка склейки:", e)
            try:
                os.replace(self.video_path, self.final_path)
            except Exception:
                pass


class App:
    def __init__(self, root):
        self.root = root
        root.title("Око — запись экрана")
        root.geometry("480x480")
        root.resizable(False, False)

        try:
            root.iconbitmap(resource_path("images.ico"))
        except Exception:
            pass

        self.recorder = None
        self.region = None
        self._busy = False

        self.videos_dir = Path.home() / "Videos"
        self.videos_dir.mkdir(exist_ok=True)

        ttk.Label(root, text="Название (необязательно):").pack(pady=(15, 5))
        self.name_entry = ttk.Entry(root, width=45)
        self.name_entry.pack(pady=5)

        self.audio_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(root, text="Записывать системный звук",
                        variable=self.audio_var).pack(pady=3)

        self.mic_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(root, text="Записывать микрофон",
                        variable=self.mic_var).pack(pady=3)

        # --- выбор области ---
        region_frame = ttk.LabelFrame(root, text="Область записи")
        region_frame.pack(pady=8, padx=20, fill="x")

        row = ttk.Frame(region_frame)
        row.pack(pady=6, padx=6, fill="x")
        ttk.Button(row, text="Выбрать область",
                   command=self.select_region).pack(side=tk.LEFT, padx=4)
        ttk.Button(row, text="Сбросить",
                   command=self.reset_region).pack(side=tk.LEFT, padx=4)
        self.region_label = ttk.Label(row, text="весь экран", foreground="gray")
        self.region_label.pack(side=tk.LEFT, padx=8)

        # --- FPS ---
        fps_frame = ttk.Frame(root)
        fps_frame.pack(pady=5)
        ttk.Label(fps_frame, text="FPS:").pack(side=tk.LEFT, padx=4)
        self.fps_var = tk.StringVar(value="20")
        ttk.Combobox(fps_frame, textvariable=self.fps_var,
                     values=["15", "20", "24", "30"], width=5,
                     state="readonly").pack(side=tk.LEFT, padx=4)

        btn_frame = ttk.Frame(root)
        btn_frame.pack(pady=10)

        self.start_btn = ttk.Button(btn_frame, text="Начать (Alt+7)",
                                    command=self.start)
        self.start_btn.pack(side=tk.LEFT, padx=5)

        self.pause_btn = ttk.Button(btn_frame, text="Пауза (Alt+8)",
                                    command=self.toggle_pause, state=tk.DISABLED)
        self.pause_btn.pack(side=tk.LEFT, padx=5)

        self.stop_btn = ttk.Button(btn_frame, text="Стоп (Alt+9)",
                                   command=self.stop, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)

        self.status = ttk.Label(root, text="Готово", foreground="blue")
        self.status.pack(pady=10)

        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._setup_hotkeys()

    # ---------- вспышка экрана ----------
    def flash_screen(self, color, duration=250):
        flash = tk.Toplevel(self.root)
        flash.overrideredirect(True)  # без рамок
        flash.attributes("-topmost", True)
        flash.attributes("-alpha", 0.3) # прозрачность 30%
        flash.configure(bg=color)
        
        sw = flash.winfo_screenwidth()
        sh = flash.winfo_screenheight()
        flash.geometry(f"{sw}x{sh}+0+0")
        
        # Автоматическое закрытие через duration мс
        flash.after(duration, flash.destroy)

    # ---------- горячие клавиши ----------
    def _setup_hotkeys(self):
        if HAS_KB:
            try:
                _kb.add_hotkey('alt+7', lambda: self.root.after(0, self._hotkey_start))
                _kb.add_hotkey('alt+8', lambda: self.root.after(0, self._hotkey_pause))
                _kb.add_hotkey('alt+9', lambda: self.root.after(0, self._hotkey_stop))
            except Exception as e:
                print("Глобальные горячие клавиши не установлены:", e)
        
        # Локальный fallback
        self.root.bind('<Alt-KeyPress-7>', lambda e: self._hotkey_start())
        self.root.bind('<Alt-KeyPress-8>', lambda e: self._hotkey_pause())
        self.root.bind('<Alt-KeyPress-9>', lambda e: self._hotkey_stop())

    def _hotkey_start(self):
        if self._busy:
            return
        try:
            self.start()
        except Exception as e:
            print("hotkey start error:", e)

    def _hotkey_pause(self):
        if not self._busy or not self.recorder or not self.recorder.recording:
            return
        try:
            self.toggle_pause()
        except Exception as e:
            print("hotkey pause error:", e)

    def _hotkey_stop(self):
        if not self._busy or not self.recorder or not self.recorder.recording:
            return
        try:
            self.stop()
        except Exception as e:
            print("hotkey stop error:", e)

    # ---------- выбор области ----------
    def select_region(self):
        self.root.withdraw()
        overlay = tk.Toplevel(self.root)
        overlay.attributes("-fullscreen", True)
        overlay.attributes("-alpha", 0.35)
        overlay.attributes("-topmost", True)
        overlay.configure(bg="black")

        canvas = tk.Canvas(overlay, cursor="cross", bg="black",
                           highlightthickness=0)
        canvas.pack(fill=tk.BOTH, expand=True)

        state = {"x0": 0, "y0": 0, "x0c": 0, "y0c": 0, "rect": None}

        def on_press(e):
            state["x0"] = e.x_root
            state["y0"] = e.y_root
            state["x0c"] = e.x
            state["y0c"] = e.y
            if state["rect"]:
                canvas.delete(state["rect"])
            state["rect"] = canvas.create_rectangle(
                e.x, e.y, e.x, e.y, outline="red", width=2
            )

        def on_drag(e):
            if state["rect"]:
                canvas.coords(state["rect"],
                              state["x0c"], state["y0c"], e.x, e.y)

        def on_release(e):
            x1 = min(state["x0"], e.x_root)
            y1 = min(state["y0"], e.y_root)
            x2 = max(state["x0"], e.x_root)
            y2 = max(state["y0"], e.y_root)
            w, h = x2 - x1, y2 - y1
            w -= w % 2
            h -= h % 2
            if w >= 40 and h >= 40:
                self.region = {
                    "left": int(x1),
                    "top": int(y1),
                    "width": int(w),
                    "height": int(h),
                }
                self.region_label.config(
                    text=f"{w}×{h} @ ({x1},{y1})", foreground="black"
                )
            close()

        def close(_e=None):
            overlay.destroy()
            self.root.deiconify()
            self.root.lift()

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        overlay.bind("<Escape>", close)
        overlay.focus_force()

    def reset_region(self):
        self.region = None
        self.region_label.config(text="весь экран", foreground="gray")

    # ---------- запуск / пауза / стоп ----------
    def start(self):
        if self._busy:
            return

        name = self.name_entry.get().strip()
        name = "".join(c for c in name if c not in r'\/:*?"<>|').strip()
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"{name}_{ts}.mp4" if name else f"recording_{ts}.mp4"
        full_path = self.videos_dir / filename

        try:
            fps = int(self.fps_var.get())
        except ValueError:
            fps = 20

        self.recorder = ScreenRecorder(
            fps=fps,
            record_audio=self.audio_var.get(),
            record_mic=self.mic_var.get(),
            region=self.region,
        )

        self._busy = True
        
        # Вспышка ЗЕЛЕНЫМ при старте
        self.flash_screen("#00FF00", 300)
        
        # Скрываем окно, чтобы не попало в кадр
        self.root.withdraw()

        try:
            self.recorder.start(str(full_path))
        except Exception as e:
            self._busy = False
          