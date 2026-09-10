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


def resource_path(rel):
    """Путь к файлу и в режиме скрипта, и внутри .exe (--onefile)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


class ScreenRecorder:
    def __init__(self, fps=20, record_audio=True, samplerate=48000):
        self.fps = fps
        self.record_audio = record_audio
        self.samplerate = samplerate
        self.recording = False
        self.paused = False
        self.video_writer = None
        self.video_thread = None
        self.audio_thread = None
        self.video_path = None
        self.audio_path = None
        self.final_path = None
        self._audio_frames = []
        self.audio_ok = False

    # ---------- запуск ----------
    def start(self, filename):
        if self.recording:
            return
        self.final_path = filename
        base, _ = os.path.splitext(filename)
        self.video_path = base + ".video.tmp.mp4"
        self.audio_path = base + ".audio.tmp.wav"
        self._audio_frames = []
        self.audio_ok = False

        self.recording = True
        self.paused = False

        self.video_thread = threading.Thread(target=self._record_video, daemon=True)
        self.video_thread.start()

        if self.record_audio:
            self.audio_thread = threading.Thread(target=self._record_audio, daemon=True)
            self.audio_thread.start()

    # ---------- видео ----------
    def _record_video(self):
        try:
            with mss.mss() as sct:
                monitor = sct.monitors[1]
                width, height = monitor["width"], monitor["height"]
                fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                self.video_writer = cv2.VideoWriter(
                    self.video_path, fourcc, self.fps, (width, height)
                )
                frame_time = 1.0 / self.fps
                while self.recording:
                    t0 = time.time()
                    if not self.paused:
                        img = np.array(sct.grab(monitor))
                        frame = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
                        self.video_writer.write(frame)
                    dt = time.time() - t0
                    if frame_time - dt > 0:
                        time.sleep(frame_time - dt)
                self.video_writer.release()
        except Exception as e:
            print("Ошибка записи видео:", e)

    # ---------- звук (системный, loopback) ----------
    def _record_audio(self):
        try:
            speaker = sc.default_speaker()
            mic = sc.get_microphone(speaker.name, include_loopback=True)
            if mic is None:
                print("Loopback-устройство не найдено")
                return
            block = 1024  # кадров за раз
            with mic.recorder(samplerate=self.samplerate, channels=2) as rec:
                self.audio_ok = True
                while self.recording:
                    if self.paused:
                        time.sleep(0.05)
                        continue
                    data = rec.record(numframes=block)
                    self._audio_frames.append(data)
        except Exception as e:
            print("Ошибка записи звука:", e)
            self.audio_ok = False

    # ---------- управление ----------
    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def stop(self):
        self.recording = False
        if self.video_thread:
            self.video_thread.join(timeout=10)
        if self.audio_thread:
            self.audio_thread.join(timeout=10)

        # Сохраняем аудио
        if self.record_audio and self._audio_frames:
            try:
                audio = np.concatenate(self._audio_frames, axis=0)
                sf.write(self.audio_path, audio, self.samplerate)
            except Exception as e:
                print("Ошибка сохранения аудио:", e)
                self.audio_path = None

        self._merge()

    # ---------- склейка ----------
    def _merge(self):
        if not os.path.exists(self.video_path):
            return

        has_audio = (
            self.record_audio
            and self.audio_path
            and os.path.exists(self.audio_path)
            and os.path.getsize(self.audio_path) > 1000
        )

        # ffmpeg ищем: сначала рядом со скриптом / внутри exe, потом в PATH
        ffmpeg = resource_path("ffmpeg.exe")
        if not os.path.exists(ffmpeg):
            ffmpeg = shutil.which("ffmpeg")

        if not has_audio or not ffmpeg:
            # просто переименовываем видео в итоговое
            try:
                os.replace(self.video_path, self.final_path)
            except Exception as e:
                print("rename error:", e)
            if has_audio and not ffmpeg:
                # оставим отдельный wav
                wav_final = os.path.splitext(self.final_path)[0] + ".wav"
                try:
                    os.replace(self.audio_path, wav_final)
                except Exception:
                    pass
                messagebox.showwarning(
                    "ffmpeg не найден",
                    "Видео сохранено без звука.\nАудио сохранено отдельным WAV-файлом."
                )
            return

        cmd = [
            ffmpeg, "-y",
            "-i", self.video_path,
            "-i", self.audio_path,
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            self.final_path,
        ]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            subprocess.run(cmd, check=True, creationflags=flags)
            os.remove(self.video_path)
            os.remove(self.audio_path)
        except Exception as e:
            print("Ошибка склейки:", e)
            # отдадим хотя бы видео
            try:
                os.replace(self.video_path, self.final_path)
            except Exception:
                pass


class App:
    def __init__(self, root):
        self.root = root
        root.title("Око — запись экрана")
        root.geometry("440x320")
        root.resizable(False, False)

        # иконка окна (работает и в .exe)
        try:
            root.iconbitmap(resource_path("images.ico"))
        except Exception:
            pass

        self.recorder = None

        self.videos_dir = Path.home() / "Videos"
        self.videos_dir.mkdir(exist_ok=True)

        ttk.Label(root, text="Название (необязательно):").pack(pady=(15, 5))
        self.name_entry = ttk.Entry(root, width=45)
        self.name_entry.pack(pady=5)

        self.audio_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(root, text="Записывать системный звук",
                        variable=self.audio_var).pack(pady=5)

        ttk.Label(root, text=f"Папка: {self.videos_dir}",
                  foreground="gray").pack(pady=(0, 10))

        btn_frame = ttk.Frame(root)
        btn_frame.pack(pady=10)

        self.start_btn = ttk.Button(btn_frame, text="Начать запись", command=self.start)
        self.start_btn.pack(side=tk.LEFT, padx=5)

        self.pause_btn = ttk.Button(btn_frame, text="Пауза",
                                    command=self.toggle_pause, state=tk.DISABLED)
        self.pause_btn.pack(side=tk.LEFT, padx=5)

        self.stop_btn = ttk.Button(btn_frame, text="Стоп",
                                   command=self.stop, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)

        self.status = ttk.Label(root, text="Готово", foreground="blue")
        self.status.pack(pady=15)

        root.protocol("WM_DELETE_WINDOW", self.on_close)

    def start(self):
        name = self.name_entry.get().strip()
        name = "".join(c for c in name if c not in r'\/:*?"<>|').strip()
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"{name}_{ts}.mp4" if name else f"recording_{ts}.mp4"
        full_path = self.videos_dir / filename

        self.recorder = ScreenRecorder(fps=20, record_audio=self.audio_var.get())
        self.recorder.start(str(full_path))

        self.start_btn.config(state=tk.DISABLED)
        self.pause_btn.config(state=tk.NORMAL, text="Пауза")
        self.stop_btn.config(state=tk.NORMAL)
        self.name_entry.config(state=tk.DISABLED)
        self.audio_var.set(self.audio_var.get())  # noop
        self.status.config(text=f"Запись: {filename}", foreground="red")

    def toggle_pause(self):
        if self.recorder.paused:
            self.recorder.resume()
            self.pause_btn.config(text="Пауза")
            self.status.config(text="Запись...", foreground="red")
        else:
            self.recorder.pause()
            self.pause_btn.config(text="Продолжить")
            self.status.config(text="Пауза", foreground="orange")

    def stop(self):
        self.status.config(text="Сохранение (склейка)...", foreground="blue")
        self.root.update_idletasks()
        self.recorder.stop()
        self.start_btn.config(state=tk.NORMAL)
        self.pause_btn.config(state=tk.DISABLED, text="Пауза")
        self.stop_btn.config(state=tk.DISABLED)
        self.name_entry.config(state=tk.NORMAL)
        self.status.config(text="Файл сохранён в папку Видео", foreground="green")

    def on_close(self):
        if self.recorder and self.recorder.recording:
            if messagebox.askyesno("Выход", "Идёт запись. Остановить и выйти?"):
                self.recorder.stop()
                self.root.destroy()
        else:
            self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()