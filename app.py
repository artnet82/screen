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


# DPI-совместимость (нужна до создания Tk), иначе выделение области
# не совпадёт с реальными пикселями на Hi-DPI экранах Windows.
if os.name == "nt":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def resource_path(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


class ScreenRecorder:
    def __init__(self, fps=20, record_audio=True, samplerate=48000, region=None):
        self.fps = fps
        self.record_audio = record_audio
        self.samplerate = samplerate
        self.region = region  # dict {'left','top','width','height'} или None
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
        self.warning = None   # текст предупреждения для UI

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
        self.warning = None

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
                if self.region:
                    monitor = {
                        "left": int(self.region["left"]),
                        "top": int(self.region["top"]),
                        "width": int(self.region["width"]),
                        "height": int(self.region["height"]),
                    }
                else:
                    monitor = sct.monitors[1]

                # H.264 требует чётных размеров — заранее подрежем.
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
                    # Пауза: накапливаем длительность, кадры не пишем.
                    if self.paused:
                        if pause_start is None:
                            pause_start = time.time()
                        time.sleep(0.03)
                        continue
                    if pause_start is not None:
                        total_pause += time.time() - pause_start
                        pause_start = None

                    # Сколько кадров ДОЛЖНО быть к этому моменту по стенным часам.
                    active = time.time() - start_time - total_pause
                    target = int(active * self.fps) + 1

                    if frame_count < target:
                        # Захватываем новый кадр.
                        try:
                            img = np.array(sct.grab(monitor))
                            frame = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
                            if frame.shape[1] != width or frame.shape[0] != height:
                                frame = frame[:height, :width]
                            last_frame = frame
                        except Exception:
                            pass

                        # Если отстаём — дублируем последний кадр, чтобы
                        # длительность видео совпадала с реальной.
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

    # ---------- звук (системный, loopback) ----------
    def _record_audio(self):
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
                    # ВАЖНО: читаем ВСЕГДА, иначе во время паузы буфер
                    # накапливается и после возобновления «выстреливает»
                    # большим куском — это и давало рассинхрон.
                    data = rec.record(numframes=block)
                    if self.paused:
                        continue
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
            self.video_thread.join(timeout=15)
        if self.audio_thread:
            self.audio_thread.join(timeout=15)

        if self.record_audio and self._audio_frames:
            try:
                audio = np.concatenate(self._audio_frames, axis=0)
                sf.write(self.audio_path, audio, self.samplerate)
            except Exception as e:
                print("Ошибка сохранения аудио:", e)
                self.audio_path = None

        self._merge()

    # ---------- склейка + перекодирование в H.264 ----------
    def _merge(self):
        if not os.path.exists(self.video_path):
            return

        has_audio = (
            self.record_audio
            and self.audio_path
            and os.path.exists(self.audio_path)
            and os.path.getsize(self.audio_path) > 1000
        )

        ffmpeg = resource_path("ffmpeg.exe")
        if not os.path.exists(ffmpeg):
            ffmpeg = shutil.which("ffmpeg")

        # Если ffmpeg недоступен — отдадим хотя бы «сырое» видео.
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
                self.warning = ("ffmpeg не найден",
                                "Видео сохранено без звука.\n"
                                "Аудио сохранено отдельным WAV-файлом.")
            return

        # Ключевые параметры сжатия:
        #   -c:v libx264  -preset veryfast  -crf 26  -> сильно меньше размер
        #   -pix_fmt yuv420p                            -> совместимость
        #   -movflags +faststart                        -> быстрый старт
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
        root.geometry("480x430")
        root.resizable(False, False)

        try:
            root.iconbitmap(resource_path("images.ico"))
        except Exception:
            pass

        self.recorder = None
        self.region = None

        self.videos_dir = Path.home() / "Videos"
        self.videos_dir.mkdir(exist_ok=True)

        ttk.Label(root, text="Название (необязательно):").pack(pady=(15, 5))
        self.name_entry = ttk.Entry(root, width=45)
        self.name_entry.pack(pady=5)

        self.audio_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(root, text="Записывать системный звук",
                        variable=self.audio_var).pack(pady=5)

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

        ttk.Label(root, text=f"Папка: {self.videos_dir}",
                  foreground="gray").pack(pady=(4, 8))

        btn_frame = ttk.Frame(root)
        btn_frame.pack(pady=6)

        self.start_btn = ttk.Button(btn_frame, text="Начать запись",
                                    command=self.start)
        self.start_btn.pack(side=tk.LEFT, padx=5)

        self.pause_btn = ttk.Button(btn_frame, text="Пауза",
                                    command=self.toggle_pause, state=tk.DISABLED)
        self.pause_btn.pack(side=tk.LEFT, padx=5)

        self.stop_btn = ttk.Button(btn_frame, text="Стоп",
                                   command=self.stop, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)

        self.status = ttk.Label(root, text="Готово", foreground="blue")
        self.status.pack(pady=12)

        root.protocol("WM_DELETE_WINDOW", self.on_close)

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
            # H.264 требует чётных размеров.
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
            region=self.region,
        )
        self.recorder.start(str(full_path))

        self.start_btn.config(state=tk.DISABLED)
        self.pause_btn.config(state=tk.NORMAL, text="Пауза")
        self.stop_btn.config(state=tk.NORMAL)
        self.name_entry.config(state=tk.DISABLED)
        self.status.config(text=f"Запись: {filename}", foreground="red")

    def toggle_pause(self):
        if not self.recorder:
            return
        if self.recorder.paused:
            self.recorder.resume()
            self.pause_btn.config(text="Пауза")
            self.status.config(text="Запись...", foreground="red")
        else:
            self.recorder.pause()
            self.pause_btn.config(text="Продолжить")
            self.status.config(text="Пауза", foreground="orange")

    def stop(self):
        if not self.recorder:
            return
        self.status.config(text="Сохранение (кодирование)...", foreground="blue")
        self.start_btn.config(state=tk.DISABLED)
        self.pause_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.DISABLED)

        # Склейка в отдельном потоке, чтобы UI не подвисал.
        def worker():
            try:
                self.recorder.stop()
            finally:
                self.root.after(0, self._stop_done)

        threading.Thread(target=worker, daemon=True).start()

    def _stop_done(self):
        self.start_btn.config(state=tk.NORMAL)
        self.pause_btn.config(state=tk.DISABLED, text="Пауза")
        self.stop_btn.config(state=tk.DISABLED)
        self.name_entry.config(state=tk.NORMAL)
        self.status.config(text="Файл сохранён в папку Видео",
                           foreground="green")

        if self.recorder and self.recorder.warning:
            title, text = self.recorder.warning
            messagebox.showwarning(title, text)
            self.recorder.warning = None

    def on_close(self):
        if self.recorder and self.recorder.recording:
            if messagebox.askyesno("Выход", "Идёт запись. Остановить и выйти?"):
                self.recorder.recording = False
                if self.recorder.video_thread:
                    self.recorder.video_thread.join(timeout=5)
                if self.recorder.audio_thread:
                    self.recorder.audio_thread.join(timeout=5)
                self.root.destroy()
        else:
            self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()        self.final_path = None
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
