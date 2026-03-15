__id__ = "flip_180"
__name__ = "Flip 180"
__description__ = "Flips the whole ExteraGram interface by 180 degrees with a soft dark intro, sound and vibration."
__version__ = "1.4.0"
__author__ = "Codex"
__min_version__ = "11.12.0"

import hashlib
import os
import threading
import urllib.request

from android_utils import log, run_on_ui_thread
from base_plugin import BasePlugin, MethodHook
from client_utils import get_last_fragment
from hook_utils import find_class
from android.content.pm import ActivityInfo
from android.content.res import Configuration
from android.graphics import Color
from android.view import Gravity, View, ViewGroup
from android.widget import FrameLayout, TextView
from ui.settings import Divider, Header, Input, Selector, Switch, Text


STREAM_NAMES = ["STREAM_SYSTEM", "STREAM_NOTIFICATION", "STREAM_MUSIC"]
VOLUME_VALUES = [0.28, 0.44, 0.62, 0.82]
OVERLAY_TAG = "flip_180_overlay"
DEFAULT_SOUND_URL = "https://zvukipro.com/uploads/files/2019-10/1570605054_281c191b3216c2e.mp3"


def safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def safe_text(value):
    return str(value or "").strip()


class LaunchResumeHook(MethodHook):
    def __init__(self, plugin):
        self.plugin = plugin

    def after_hooked_method(self, param):
        activity = getattr(param, "thisObject", None)
        run_on_ui_thread(lambda: self.plugin._ensure_activity_state(activity))


class Plugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self._unhooks = []
        self._previous_orientation = None
        self._tone_class = None
        self._audio_manager_class = None
        self._context_class = None
        self._build_version_class = None
        self._vibration_effect_class = None
        self._media_player_class = None
        self._intro_done = False
        self._player = None
        self._sound_cache_thread = None
        self._sound_cache_path = None

    def _load_feedback_classes(self):
        if self._context_class is None:
            self._context_class = find_class("android.content.Context")
        if self._build_version_class is None:
            self._build_version_class = find_class("android.os.Build$VERSION")
        if self._vibration_effect_class is None:
            self._vibration_effect_class = find_class("android.os.VibrationEffect")
        if self._tone_class is None:
            self._tone_class = find_class("android.media.ToneGenerator")
        if self._audio_manager_class is None:
            self._audio_manager_class = find_class("android.media.AudioManager")
        if self._media_player_class is None:
            self._media_player_class = find_class("android.media.MediaPlayer")

    def on_plugin_load(self):
        try:
            launch_activity = find_class("org.telegram.ui.LaunchActivity")
            if launch_activity is not None:
                hooks = self.hook_all_methods(launch_activity, "onResume", LaunchResumeHook(self))
                if hooks:
                    if isinstance(hooks, (list, tuple)):
                        self._unhooks.extend(hooks)
                    else:
                        self._unhooks.append(hooks)
        except Exception as e:
            log(f"[Flip180] hook error: {e}")

        self._prefetch_sound_async()
        delay = max(0, min(10000, safe_int(self.get_setting("intro_delay_ms", "2000"), 2000)))
        run_on_ui_thread(lambda: self._start_intro_sequence(force=True), delay=delay)

    def on_plugin_unload(self):
        try:
            for hook in self._unhooks:
                try:
                    if hook:
                        hook.unhook()
                except Exception:
                    pass
        finally:
            self._unhooks = []
        self._intro_done = False
        run_on_ui_thread(lambda: self._restore_current(animate=False))
        run_on_ui_thread(self._stop_sound)
        run_on_ui_thread(lambda: self._remove_overlay(self._current_activity()))

    def create_settings(self):
        return [
            Header(text="Flip 180"),
            Input(
                key="intro_delay_ms",
                text="Задержка перед эффектом (мс)",
                default="2000",
                subtext="Сколько ждать после включения плагина.",
            ),
            Input(
                key="fade_in_ms",
                text="Затемнение до чёрного (мс)",
                default="260",
                subtext="Как быстро экран уйдёт в полный чёрный.",
            ),
            Input(
                key="fade_out_ms",
                text="Мягкое открытие экрана (мс)",
                default="520",
                subtext="Как быстро откроется уже перевёрнутый экран.",
            ),
            Input(
                key="smiley_delay_ms",
                text="Пауза до скобки (мс)",
                default="1000",
                subtext="Сколько держать просто чёрный экран до появления скобки.",
            ),
            Input(
                key="smiley_show_ms",
                text="Сколько держать скобку (мс)",
                default="1000",
                subtext="Как долго скобка остаётся на экране.",
            ),
            Input(
                key="pre_reveal_ms",
                text="Пауза после скобки (мс)",
                default="1000",
                subtext="Сколько ждать после исчезновения скобки до открытия перевёрнутого экрана.",
            ),
            Input(
                key="smiley_text",
                text="Символ по центру",
                default=")",
                subtext="Например: ), :), ^_^",
            ),
            Switch(
                key="animate_on_resume",
                text="Держать переворот при возвращении",
                default=True,
                subtext="Без повторного интро, просто сохраняет upside-down состояние.",
            ),
            Divider(),
            Header(text="Звук"),
            Switch(
                key="sound_enabled",
                text="Включить звук",
                default=True,
                subtext="Пытается играть звук из URL, потом падает в встроенный тон.",
            ),
            Input(
                key="sound_url",
                text="URL звука",
                default=DEFAULT_SOUND_URL,
                subtext="Можно заменить на другой прямой mp3 URL.",
            ),
            Selector(
                key="sound_stream",
                text="Аудиопоток fallback-тона",
                default=0,
                items=["System", "Notification", "Music"],
                icon="msg_info",
            ),
            Selector(
                key="sound_volume",
                text="Громкость fallback-тона",
                default=2,
                items=["Тихо", "Нормально", "Громко", "Очень громко"],
                icon="msg_info",
            ),
            Divider(),
            Header(text="Вибрация"),
            Switch(
                key="vibration_enabled",
                text="Включить вибрацию",
                default=True,
                subtext="Короткий haptic в начале интро и в момент переворота.",
            ),
            Input(
                key="vibration_ms",
                text="Длительность вибрации (мс)",
                default="24",
                subtext="Длина одного сигнала.",
                icon="msg_info",
            ),
            Divider(),
            Text(
                text="Повторить интро сейчас",
                subtext="Снова показать мягкое затемнение и открыть экран upside-down.",
                on_click=lambda _view: run_on_ui_thread(lambda: self._start_intro_sequence(force=True)),
            ),
            Text(
                text="Вернуть нормальный вид",
                subtext="Снять переворот и убрать оверлей.",
                on_click=lambda _view: run_on_ui_thread(lambda: self._restore_current(animate=False)),
            ),
        ]

    def _delay_ms(self):
        return max(0, min(10000, safe_int(self.get_setting("intro_delay_ms", "2000"), 2000)))

    def _fade_in_ms(self):
        return max(80, min(5000, safe_int(self.get_setting("fade_in_ms", "520"), 520)))

    def _fade_out_ms(self):
        return max(80, min(5000, safe_int(self.get_setting("fade_out_ms", "520"), 520)))

    def _smiley_delay_ms(self):
        return max(0, min(6000, safe_int(self.get_setting("smiley_delay_ms", "1000"), 1000)))

    def _smiley_show_ms(self):
        return max(120, min(6000, safe_int(self.get_setting("smiley_show_ms", "1000"), 1000)))

    def _pre_reveal_ms(self):
        return max(0, min(6000, safe_int(self.get_setting("pre_reveal_ms", "1000"), 1000)))

    def _feedback_duration(self):
        return max(10, min(250, safe_int(self.get_setting("vibration_ms", "24"), 24)))

    def _current_activity(self):
        fragment = get_last_fragment()
        if fragment is None:
            return None
        try:
            return fragment.getParentActivity()
        except Exception:
            return None

    def _ensure_activity_state(self, activity):
        if activity is None:
            return
        if self.get_setting("animate_on_resume", True):
            self._ensure_orientation(activity)
        self._remove_stale_rotation(activity)

    def _remove_stale_rotation(self, activity):
        try:
            window = activity.getWindow()
            decor = window.getDecorView() if window else None
            if decor is not None:
                decor.setRotation(0.0)
        except Exception:
            pass

    def _target_orientation(self, activity):
        try:
            orientation = activity.getResources().getConfiguration().orientation
            if orientation == Configuration.ORIENTATION_LANDSCAPE:
                return ActivityInfo.SCREEN_ORIENTATION_REVERSE_LANDSCAPE
        except Exception:
            pass
        return ActivityInfo.SCREEN_ORIENTATION_REVERSE_PORTRAIT

    def _normal_orientation(self):
        if self._previous_orientation is None:
            return ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED
        return self._previous_orientation

    def _ensure_orientation(self, activity):
        try:
            target = self._target_orientation(activity)
            current = safe_int(activity.getRequestedOrientation(), ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED)
            if current != target:
                if self._previous_orientation is None:
                    self._previous_orientation = current
                activity.setRequestedOrientation(target)
        except Exception as e:
            log(f"[Flip180] ensure orientation error: {e}")

    def _restore_current(self, animate=False):
        activity = self._current_activity()
        if activity is None:
            return
        try:
            activity.setRequestedOrientation(self._normal_orientation())
            self._remove_overlay(activity)
            self._remove_stale_rotation(activity)
        except Exception as e:
            log(f"[Flip180] restore error: {e}")

    def _resolve_stream_value(self):
        self._load_feedback_classes()
        index = max(0, min(len(STREAM_NAMES) - 1, safe_int(self.get_setting("sound_stream", 0), 0)))
        stream_name = STREAM_NAMES[index]
        try:
            return getattr(self._audio_manager_class, stream_name)
        except Exception:
            try:
                return getattr(self._audio_manager_class, "STREAM_SYSTEM")
            except Exception:
                return 1

    def _resolve_tone_volume(self):
        index = max(0, min(len(VOLUME_VALUES) - 1, safe_int(self.get_setting("sound_volume", 2), 2)))
        return int(VOLUME_VALUES[index] * 100)

    def _plugin_dir(self):
        try:
            return os.path.dirname(os.path.abspath(__file__))
        except Exception:
            return "/data/user/0/com.exteragram.messenger/files/plugins"

    def _sound_cache_file(self, url):
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        return os.path.join(self._plugin_dir(), f"flip_180_sound_{digest}.mp3")

    def _prefetch_sound_async(self):
        if not self.get_setting("sound_enabled", True):
            return
        url = safe_text(self.get_setting("sound_url", DEFAULT_SOUND_URL))
        if not url:
            return
        expected_path = self._sound_cache_file(url)
        if self._sound_cache_path == expected_path and os.path.exists(expected_path):
            return
        if self._sound_cache_thread and self._sound_cache_thread.is_alive():
            return

        def _download():
            try:
                os.makedirs(self._plugin_dir(), exist_ok=True)
                tmp_path = expected_path + ".part"
                with urllib.request.urlopen(url, timeout=8) as response, open(tmp_path, "wb") as out:
                    out.write(response.read())
                if os.path.getsize(tmp_path) > 1024:
                    os.replace(tmp_path, expected_path)
                    self._sound_cache_path = expected_path
                else:
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
            except Exception as e:
                log(f"[Flip180] sound prefetch error: {e}")

        self._sound_cache_thread = threading.Thread(target=_download, name="flip180_sound_prefetch", daemon=True)
        self._sound_cache_thread.start()

    def _get_vibrator(self, activity):
        self._load_feedback_classes()
        if activity is None or self._context_class is None:
            return None
        try:
            return activity.getSystemService(self._context_class.VIBRATOR_SERVICE)
        except Exception:
            return None

    def _vibrate(self, activity):
        if not self.get_setting("vibration_enabled", True):
            return
        vibrator = self._get_vibrator(activity)
        if vibrator is None:
            return
        duration = self._feedback_duration()
        try:
            if self._build_version_class and self._build_version_class.SDK_INT >= 26 and self._vibration_effect_class:
                effect = self._vibration_effect_class.createOneShot(duration, self._vibration_effect_class.DEFAULT_AMPLITUDE)
                vibrator.vibrate(effect)
            else:
                vibrator.vibrate(duration)
        except Exception as e:
            log(f"[Flip180] vibrate error: {e}")

    def _stop_sound(self):
        player = self._player
        self._player = None
        if player is None:
            return
        try:
            player.stop()
        except Exception:
            pass
        try:
            player.release()
        except Exception:
            pass

    def _play_fallback_tone(self):
        self._load_feedback_classes()
        if not self._tone_class or not self._audio_manager_class:
            return
        try:
            generator = self._tone_class(self._resolve_stream_value(), self._resolve_tone_volume())
            tone = getattr(self._tone_class, "TONE_PROP_BEEP2")
            generator.startTone(tone, 180)
        except Exception as e:
            log(f"[Flip180] fallback tone error: {e}")

    def _play_sound(self):
        if not self.get_setting("sound_enabled", True):
            return
        self._stop_sound()
        url = safe_text(self.get_setting("sound_url", DEFAULT_SOUND_URL))
        if not url:
            self._play_fallback_tone()
            return
        self._load_feedback_classes()
        if self._media_player_class is None:
            self._play_fallback_tone()
            return
        try:
            player = self._media_player_class()
            local_path = self._sound_cache_file(url)
            if not os.path.exists(local_path):
                self._prefetch_sound_async()
                self._play_fallback_tone()
                return
            self._sound_cache_path = local_path
            player.setDataSource(local_path)
            player.setAudioStreamType(self._resolve_stream_value())
            try:
                volume = VOLUME_VALUES[max(0, min(len(VOLUME_VALUES) - 1, safe_int(self.get_setting("sound_volume", 2), 2)))]
                player.setVolume(volume, volume)
            except Exception:
                pass
            player.setOnCompletionListener(None)
            player.prepare()
            player.start()
            self._player = player
        except Exception as e:
            log(f"[Flip180] media sound error: {e}")
            self._play_fallback_tone()

    def _find_overlay(self, activity):
        if activity is None:
            return None
        try:
            window = activity.getWindow()
            decor = window.getDecorView() if window else None
            if decor is None or not isinstance(decor, ViewGroup):
                return None
            for index in range(decor.getChildCount() - 1, -1, -1):
                child = decor.getChildAt(index)
                if child is not None and OVERLAY_TAG == child.getTag():
                    return child
        except Exception:
            pass
        return None

    def _remove_overlay(self, activity):
        overlay = self._find_overlay(activity)
        if overlay is None:
            return
        try:
            parent = overlay.getParent()
            if parent is not None:
                parent.removeView(overlay)
        except Exception:
            pass

    def _make_overlay(self, activity):
        self._remove_overlay(activity)
        window = activity.getWindow()
        decor = window.getDecorView() if window else None
        if decor is None or not isinstance(decor, ViewGroup):
            return None

        overlay = FrameLayout(activity)
        overlay.setTag(OVERLAY_TAG)
        overlay.setClickable(True)
        overlay.setFocusable(True)
        overlay.setAlpha(0.0)
        overlay.setBackgroundColor(Color.parseColor("#FF000000"))

        smile = TextView(activity)
        smile.setText(safe_text(self.get_setting("smiley_text", ")")) or ")")
        smile.setTextColor(Color.WHITE)
        smile.setTextSize(56.0)
        smile.setAlpha(0.0)
        smile.setGravity(Gravity.CENTER)

        params = FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)
        overlay.addView(smile, params)
        decor.addView(overlay, params)
        return overlay, smile

    def _start_intro_sequence(self, force=False):
        activity = self._current_activity()
        if activity is None:
            return
        if self._intro_done and not force:
            self._ensure_orientation(activity)
            return
        self._intro_done = True

        target = self._target_orientation(activity)
        current = safe_int(activity.getRequestedOrientation(), ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED)
        if self._previous_orientation is None:
            self._previous_orientation = current

        made = self._make_overlay(activity)
        if not made:
            self._play_sound()
            self._vibrate(activity)
            activity.setRequestedOrientation(target)
            return
        overlay, smile = made

        fade_in = self._fade_in_ms()
        fade_out = self._fade_out_ms()
        smiley_delay = self._smiley_delay_ms()
        smiley_show = self._smiley_show_ms()
        pre_reveal = self._pre_reveal_ms()
        self._play_sound()
        self._vibrate(activity)

        try:
            overlay.animate().alpha(1.0).setDuration(fade_in).start()
        except Exception:
            overlay.setAlpha(1.0)

        def _show_smiley():
            try:
                smile.animate().alpha(1.0).setDuration(180).start()
            except Exception:
                smile.setAlpha(1.0)

            def _hide_smiley():
                try:
                    smile.animate().alpha(0.0).setDuration(180).start()
                except Exception:
                    smile.setAlpha(0.0)

                def _flip_under_overlay():
                    self._vibrate(activity)
                    try:
                        activity.setRequestedOrientation(target)
                    except Exception as e:
                        log(f"[Flip180] orientation switch error: {e}")

                    def _reveal():
                        try:
                            overlay.animate().alpha(0.0).setDuration(fade_out).start()
                            smile.animate().alpha(0.0).setDuration(fade_out).start()
                        except Exception:
                            overlay.setAlpha(0.0)
                            smile.setAlpha(0.0)
                        run_on_ui_thread(lambda: self._remove_overlay(activity), delay=fade_out + 60)

                    run_on_ui_thread(_reveal, delay=pre_reveal)

                run_on_ui_thread(_flip_under_overlay, delay=180)

            run_on_ui_thread(_hide_smiley, delay=smiley_show)

        run_on_ui_thread(_show_smiley, delay=fade_in + smiley_delay)
