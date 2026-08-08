"""
视频格式转换 - 支持多种视频格式互转
支持格式: MP4 | MKV | AVI | MOV | WEBM | GIF | M4V | FLV | WMV | TS
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import List

# ── 路径与模板 ──
BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
CONFIG_TEMPLATE = BASE_DIR / "config.html"
APP_DATA_MARKER = "/*__APP_DATA__*/"

# ==================== 视频格式定义 ====================
# 所有输出格式
VIDEO_FORMATS = [
    "mp4", "mkv", "avi", "mov", "webm", "gif",
    "m4v", "flv", "wmv", "ts",
]

# 格式 → 推荐视频编码器（auto 时使用）
VIDEO_CODECS = {
    "mp4": "libx264",
    "mkv": "libx264",
    "avi": "mpeg4",
    "mov": "libx264",
    "webm": "libvpx-vp9",
    "gif": "gif",
    "m4v": "libx264",
    "flv": "libx264",
    "wmv": "wmv2",
    "ts": "libx264",
}

# 格式 → 推荐音频编码器（encode 模式使用）
AUDIO_CODECS = {
    "mp4": "aac",
    "mkv": "aac",
    "avi": "libmp3lame",
    "mov": "aac",
    "webm": "libopus",
    "gif": None,
    "m4v": "aac",
    "flv": "aac",
    "wmv": "wmav2",
    "ts": "aac",
}

# encode 模式下各音频编码器的默认比特率
AUDIO_BITRATES = {
    "mp4": "192k",
    "mkv": "192k",
    "avi": "128k",
    "mov": "192k",
    "webm": "128k",
    "m4v": "192k",
    "flv": "192k",
    "wmv": "192k",
    "ts": "192k",
}

# 各容器可安全复制(re-mux)的音频编码器；copy 模式下源音轨不在集合内则自动降级为重新编码
COPY_SAFE_AUDIO = {
    "webm": {"opus", "vorbis"},
    "wmv": {"wmav2", "wmav1", "wmapro", "wmalossless"},
    "flv": {"aac", "mp3", "mp2"},
}

# 用户可选视频编码器（auto = 跟随格式推荐）
ALLOWED_VIDEO_CODECS = {"auto", "libx264", "libx265", "libvpx-vp9", "mpeg4"}

# 分辨率选项（"original" = 跟随输入）
RESOLUTIONS = {"480p": 480, "720p": 720, "1080p": 1080}

# 帧率选项（"original" = 跟随输入）
ALLOWED_FPS = {"original", "24", "25", "30", "50", "60"}

# 画质选项
ALLOWED_QUALITIES = {"best", "high", "standard", "low", "small"}
# CRF 类编码器（libx264 / libx265）
CRF_QUALITY = {"best": 18, "high": 20, "standard": 23, "low": 26, "small": 28}
# VP9 单独一套 CRF（数值与 x264 不通用）
VP9_QUALITY = {"best": 25, "high": 30, "standard": 35, "low": 40, "small": 45}
# qscale 类编码器（mpeg4 / wmv2），-q:v，越小越好
QSCALE_QUALITY = {"best": 2, "high": 4, "standard": 5, "low": 7, "small": 9}

CRF_CODECS = {"libx264", "libx265", "libvpx-vp9"}
QSCALE_CODECS = {"mpeg4", "wmv2"}

# 音频处理模式（copy = 保留原音轨 / encode = 重新编码 / none = 移除）
ALLOWED_AUDIO_MODES = {"copy", "encode", "none"}

# 确定不兼容的编码器组合
WEBM_VIDEO_CODECS = {"auto", "libvpx-vp9"}
WMV_VIDEO_CODECS = {"auto", "wmv2"}

# GIF 特殊处理：原始帧率兜底 + 原始分辨率最大宽度（避免文件过大）
GIF_DEFAULT_FPS = "15"
GIF_MAX_WIDTH = 480

# 下拉选项的显示文案（dict 顺序即下拉顺序）
VIDEO_CODEC_OPTIONS = {
    "auto": "自动 (跟随格式)",
    "libx264": "H.264 (兼容性最佳)",
    "libx265": "H.265/HEVC (高压缩)",
    "libvpx-vp9": "VP9 (WebM 常用)",
    "mpeg4": "MPEG-4 (兼容老设备)",
}

RESOLUTION_OPTIONS = {
    "original": "原始 (跟随输入)",
    "480p": "480p (标清)",
    "720p": "720p (高清)",
    "1080p": "1080p (全高清)",
}

QUALITY_OPTIONS = {
    "best": "最佳 (CRF 18)",
    "high": "高 (CRF 20)",
    "standard": "标准 (CRF 23)",
    "low": "较低 (CRF 26)",
    "small": "最小 (CRF 28)",
}

FPS_OPTIONS = {
    "original": "原始 (跟随输入)",
    "24": "24 fps",
    "25": "25 fps",
    "30": "30 fps",
    "50": "50 fps",
    "60": "60 fps",
}

AUDIO_MODE_OPTIONS = {
    "copy": "保留原音轨 (最快)",
    "encode": "重新编码 (推荐编码器)",
    "none": "移除音轨",
}

DEFAULT_CONFIG = {
    "format": "mp4",
    "output_dir": "",
    "video_codec": "auto",
    "resolution": "original",
    "quality": "standard",
    "fps": "original",
    "audio_mode": "copy",
    "overwrite": False,
}

# 右键可选的视频扩展名（与 bm-scripts-box-rc.toml 的 filters 一致）
VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".ts", ".m4v",
    ".mpg", ".mpeg", ".m2ts", ".mts", ".3gp", ".ogv", ".vob", ".rmvb", ".rm", ".asf",
}


class VideoConverter:
    """视频格式转换器（基于 FFmpeg），承载通用 subprocess 执行，只产数据。"""

    @staticmethod
    def _run(cmd, **kw):
        """执行命令，默认隐藏控制台窗口、按 UTF-8 容错解码。"""
        kw.setdefault("creationflags", getattr(subprocess, "CREATE_NO_WINDOW", 0))
        kw.setdefault("encoding", "utf-8")
        return subprocess.run(cmd, text=True, errors="replace", **kw)

    @staticmethod
    def _require_binaries():
        """同时校验 ffmpeg 与 ffprobe，缺则直接报错（供 Cli 开局预检）。"""
        if not shutil.which("ffmpeg"):
            raise FileNotFoundError("未找到 FFmpeg，请安装并加入环境变量 PATH（https://ffmpeg.org/download.html）")
        if not shutil.which("ffprobe"):
            raise FileNotFoundError("未找到 ffprobe，请确认已完整安装 FFmpeg（含 ffprobe）并加入 PATH")

    def __init__(self, videos, config):
        """videos: 视频文件路径列表；config: 配置 dict（见 DEFAULT_CONFIG）。"""
        self.videos = [v for v in videos if Path(v).exists()]
        c = config

        self.output_dir = str(c.get("output_dir") or "").strip()
        self.output_format = str(c.get("format") or "mp4").strip().lstrip(".").lower()
        if self.output_format not in VIDEO_FORMATS:
            raise ValueError(f"不支持的格式：{self.output_format}，支持：{', '.join(VIDEO_FORMATS)}")

        # 非法参数值回退默认
        self.video_codec = c.get("video_codec") if c.get("video_codec") in ALLOWED_VIDEO_CODECS else "auto"
        self.resolution = c.get("resolution") if c.get("resolution") in {"original", *RESOLUTIONS} else "original"
        self.quality = c.get("quality") if c.get("quality") in ALLOWED_QUALITIES else "standard"
        self.fps = c.get("fps") if c.get("fps") in ALLOWED_FPS else "original"
        self.audio_mode = c.get("audio_mode") if c.get("audio_mode") in ALLOWED_AUDIO_MODES else "copy"
        self.overwrite = bool(c.get("overwrite"))

        self._require_binaries()
        self._ffmpeg = shutil.which("ffmpeg")
        self._ffprobe = shutil.which("ffprobe")

        if self.output_dir:
            os.makedirs(self.output_dir, exist_ok=True)

    def _get_audio_codec(self, input_path: str):
        """探测输入文件第一条音轨的编码器，无音轨或探测失败返回 None。"""
        if not self._ffprobe:
            return None
        try:
            proc = self._run(
                [self._ffprobe, "-v", "error", "-select_streams", "a:0",
                 "-show_entries", "stream=codec_name",
                 "-of", "default=nw=1:nk=1", input_path],
                capture_output=True,
            )
            if proc.returncode == 0:
                codec = proc.stdout.strip()
                return codec or None
        except Exception:
            pass
        return None

    def _get_duration(self, input_path: str):
        """探测输入视频总时长（秒），失败返回 None。"""
        if not self._ffprobe:
            return None
        try:
            proc = self._run(
                [self._ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", input_path],
                capture_output=True,
            )
            if proc.returncode == 0:
                try:
                    return float(proc.stdout.strip())
                except ValueError:
                    return None
        except Exception:
            pass
        return None

    def _get_output_path(self, input_path: str) -> str:
        """生成输出文件路径（输出目录留空则写到源文件所在目录）。"""
        stem = Path(input_path).stem
        ext = self.output_format
        base = self.output_dir or str(Path(input_path).parent)
        return os.path.join(base, f"{stem}.{ext}")

    def _validate(self):
        """编码器与容器兼容性预校验，返回错误信息字符串，None 表示通过。"""
        fmt, vc = self.output_format, self.video_codec
        if fmt == "webm" and vc not in WEBM_VIDEO_CODECS:
            return f"WebM 容器仅支持 VP9 视频编码，当前选择 {vc} 不兼容"
        if fmt == "wmv" and vc not in WMV_VIDEO_CODECS:
            return f"WMV 容器仅支持 wmv2 视频编码，当前选择 {vc} 不兼容"
        return None

    def _build_gif_filter(self) -> str:
        """构建 GIF 的调色板滤镜链（split + palettegen + paletteuse，复杂滤镜图）"""
        fps = self.fps if self.fps != "original" else GIF_DEFAULT_FPS
        chain = [f"fps={fps}"]
        if self.resolution != "original":
            chain.append(f"scale=-2:{RESOLUTIONS[self.resolution]}:flags=lanczos")
        else:
            # 原始分辨率时限制最大宽度，避免 GIF 文件过大
            chain.append(f"scale='min(iw,{GIF_MAX_WIDTH})':-1:flags=lanczos")
        chain.append("split[s0][s1]")
        first = ",".join(chain)
        return ";".join([first, "[s0]palettegen=stats_mode=diff[p]", "[s1][p]paletteuse[vout]"])

    def _build_command(self, input_path: str, output_path: str) -> List[str]:
        """构建 ffmpeg 命令"""
        fmt = self.output_format
        cmd = [self._ffmpeg, "-y", "-i", input_path]

        if fmt == "gif":
            # GIF：调色板复杂滤镜图 + 映射滤镜输出 + 无限循环
            cmd += ["-filter_complex", self._build_gif_filter()]
            cmd += ["-map", "[vout]"]
            cmd += ["-an"]
            cmd += ["-loop", "0"]
        else:
            # 显式只取第一条视频流 + 可选第一条音轨
            cmd += ["-map", "0:v:0"]

            if self.audio_mode == "copy":
                cmd += ["-map", "0:a:0?"]
                # 严格容器：源音轨不兼容时自动降级为重新编码，避免 muxer 报错
                if fmt in COPY_SAFE_AUDIO:
                    src_ac = self._get_audio_codec(input_path)
                    if src_ac and src_ac not in COPY_SAFE_AUDIO[fmt]:
                        cmd += ["-c:a", AUDIO_CODECS[fmt], "-b:a", AUDIO_BITRATES[fmt]]
                    else:
                        cmd += ["-c:a", "copy"]
                else:
                    cmd += ["-c:a", "copy"]
            elif self.audio_mode == "encode":
                cmd += ["-map", "0:a:0?"]
                cmd += ["-c:a", AUDIO_CODECS[fmt]]
                cmd += ["-b:a", AUDIO_BITRATES[fmt]]
            else:  # none
                cmd += ["-an"]

            # 视频编码器（auto → 格式推荐）
            vcodec = VIDEO_CODECS[fmt] if self.video_codec == "auto" else self.video_codec
            cmd += ["-c:v", vcodec]

            # 画质：按编码器类别选参数
            if vcodec in CRF_CODECS:
                if vcodec == "libvpx-vp9":
                    # vp9 需 -b:v 0 才是纯 CRF 模式
                    cmd += ["-b:v", "0"]
                    cmd += ["-crf", str(VP9_QUALITY[self.quality])]
                    cmd += ["-deadline", "good", "-cpu-used", "2", "-row-mt", "1"]
                else:
                    cmd += ["-crf", str(CRF_QUALITY[self.quality])]
            elif vcodec in QSCALE_CODECS:
                cmd += ["-q:v", str(QSCALE_QUALITY[self.quality])]

            # 像素格式兼容（保证最广播放器支持）
            if vcodec in ("libx264", "libx265", "libvpx-vp9"):
                cmd += ["-pix_fmt", "yuv420p"]

            # HEVC 苹果兼容标签（仅 mp4/mov；m4v 的 ipod muxer 不支持 HEVC）
            if vcodec == "libx265" and fmt in ("mp4", "mov"):
                cmd += ["-tag:v", "hvc1"]

            # 分辨率（滤镜缩放，偶数宽度）
            if self.resolution != "original":
                cmd += ["-vf", f"scale=-2:{RESOLUTIONS[self.resolution]}"]

            # 帧率（输出选项，滤镜之后生效）
            if self.fps != "original":
                cmd += ["-r", self.fps]

        # 保留元数据，失败时输出错误信息
        cmd += ["-map_metadata", "0", "-loglevel", "error"]
        cmd.append(output_path)

        return cmd

    def _convert_single(self, video_path: str, on_start=None, on_progress=None) -> tuple:
        """转换单个视频，返回 (路径, 状态, 信息)，状态: success/skipped/failed。"""
        try:
            # 编码器与容器兼容性校验
            error = self._validate()
            if error:
                return video_path, "failed", error

            output_path = self._get_output_path(video_path)

            # 同格式跳过
            input_ext = Path(video_path).suffix.lstrip(".").lower()
            if input_ext == self.output_format:
                return video_path, "skipped", "同格式无需转换"

            # 已存在跳过
            if os.path.exists(output_path) and not self.overwrite:
                return video_path, "skipped", "文件已存在"

            cmd = self._build_command(video_path, output_path)

            if on_start:
                on_start(video_path)

            # 探测总时长，用于实时百分比
            total_us = None
            if on_progress:
                duration = self._get_duration(video_path)
                if duration:
                    total_us = duration * 1_000_000

            # -progress 将编码进度输出到 stdout，错误信息仍走 stderr
            cmd += ["-nostats", "-progress", "pipe:1"]

            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                errors="replace", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

            # 后台线程排空 stderr，避免管道满阻塞
            stderr_lines = []

            def _read_stderr():
                try:
                    for line in proc.stderr:
                        stderr_lines.append(line)
                except Exception:
                    pass

            stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
            stderr_thread.start()

            # 实时解析 stdout 进度（out_time_us 为微秒）
            last_pct = -1.0
            for line in proc.stdout:
                if not line.startswith("out_time_us="):
                    continue
                try:
                    cur_us = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                pct = min(100.0, cur_us / total_us * 100.0) if total_us else None
                # 按 0.5% 粒度回调，避免刷屏
                if pct is None or pct - last_pct >= 0.5 or pct >= 100.0:
                    last_pct = pct
                    secs = cur_us / 1_000_000
                    time_str = f"{int(secs // 3600):02d}:{int(secs % 3600 // 60):02d}:{secs % 60:04.1f}"
                    if on_progress:
                        on_progress(pct, time_str)

            proc.wait()
            stderr_thread.join()

            if proc.returncode != 0:
                return video_path, "failed", ("".join(stderr_lines).strip() or "转换失败")
            return video_path, "success", output_path

        except Exception as e:
            return video_path, "failed", str(e)

    def convert(self, on_start=None, on_progress=None, on_done=None) -> dict:
        """顺序转换全部视频，回调供 Cli 展示；返回分组结果 dict。"""
        results = {"success": [], "skipped": [], "failed": [], "total": len(self.videos)}

        for path in self.videos:
            path, status, info = self._convert_single(path, on_start=on_start, on_progress=on_progress)
            if status == "success":
                results["success"].append((path, info))
            elif status == "skipped":
                results["skipped"].append((path, info))
            else:
                results["failed"].append((path, info))
            if on_done:
                on_done(path, status, info)

        return results

    @staticmethod
    def get_file_size(path: str) -> str:
        """获取文件大小（人性化显示）"""
        size = os.path.getsize(path)
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"


class Gui:
    """webview-cli 配置窗口（含配置的读写与校验）。"""

    @staticmethod
    def _render(data):
        """读取 HTML 模板并注入 APP_DATA（常量单一来源在 Python）。"""
        payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
        html = CONFIG_TEMPLATE.read_text(encoding="utf-8")
        return html.replace(APP_DATA_MARKER, f"const APP_DATA = {payload};")

    @staticmethod
    def _webview_bin():
        webview = shutil.which("webview-cli") or shutil.which("webview")
        if not webview:
            raise FileNotFoundError(
                "未找到 webview-cli，请确认已安装并加入 PATH\n"
                "https://github.com/just-be-dev/webview-cli"
            )
        return webview

    @staticmethod
    def _validate(data):
        """校验配置窗口返回的数据（镜像 HTML 里的 JS 规则），返回规范化后的 dict。"""
        if not isinstance(data, dict):
            raise ValueError("返回的数据格式无效")

        fmt = str(data.get("format") or "").strip().lstrip(".").lower()
        if fmt not in VIDEO_FORMATS:
            raise ValueError(f"请选择有效的输出格式（支持：{', '.join(VIDEO_FORMATS)}）")
        data["format"] = fmt  # 归一化后落盘

        if data.get("video_codec") not in ALLOWED_VIDEO_CODECS:
            raise ValueError(f"无效的视频编码器：{data.get('video_codec')}")
        if data.get("resolution") not in {"original", *RESOLUTIONS}:
            raise ValueError(f"无效的分辨率：{data.get('resolution')}")
        if data.get("quality") not in ALLOWED_QUALITIES:
            raise ValueError(f"无效的画质：{data.get('quality')}")
        if data.get("fps") not in ALLOWED_FPS:
            raise ValueError(f"无效的帧率：{data.get('fps')}")
        if data.get("audio_mode") not in ALLOWED_AUDIO_MODES:
            raise ValueError(f"无效的音频模式：{data.get('audio_mode')}")

        data.update(
            output_dir=str(data.get("output_dir") or "").strip(),
            overwrite=bool(data.get("overwrite")),
        )
        # 补齐默认字段，保证 config.json 全字段、下游解析安全
        for k, v in DEFAULT_CONFIG.items():
            if k not in data:
                data[k] = v
        return data

    @staticmethod
    def load_config():
        """读取配置；缺失/损坏/非法返回 None（触发首次引导）。"""
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        return data if data.get("format") in VIDEO_FORMATS else None

    @staticmethod
    def save_config(data):
        CONFIG_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def ask(self):
        """打开配置窗口，返回校验后的配置 dict；取消/出错返回 None。"""
        webview = self._webview_bin()
        data = {"saved": self.load_config() or {},
                "DEFAULTS": DEFAULT_CONFIG,
                "VIDEO_FORMATS": VIDEO_FORMATS,
                "VIDEO_CODEC_OPTIONS": VIDEO_CODEC_OPTIONS,
                "RESOLUTION_OPTIONS": RESOLUTION_OPTIONS,
                "QUALITY_OPTIONS": QUALITY_OPTIONS,
                "FPS_OPTIONS": FPS_OPTIONS,
                "AUDIO_MODE_OPTIONS": AUDIO_MODE_OPTIONS}
        html = self._render(data)
        cmd = [webview, "--title", "视频格式转换 - 配置窗口", "--width", "480", "--height", "720"]
        try:
            proc = VideoConverter._run(cmd, input=html, capture_output=True)
        except (OSError, ValueError):
            # stdin 管道不可用时回退到临时 HTML 文件
            fd, path = tempfile.mkstemp(suffix=".html", prefix="video-format-webview-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(html)
                proc = VideoConverter._run(cmd + [path], input="", capture_output=True)
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
        if proc.returncode:
            if proc.returncode != 2 and (proc.stderr or "").strip():
                print((proc.stderr or "").strip())
            return None  # 取消(2) / 出错
        try:
            payload = json.loads(proc.stdout)
        except ValueError:
            print("配置窗口返回的数据无法解析")
            return None
        try:
            return self._validate(payload)
        except (ValueError, TypeError) as e:
            print(f"配置校验失败：{e}")
            return None


class Cli:
    """批处理命令行流程（含盒子参数解析与输出编码修复）。"""

    @staticmethod
    def _fix_encoding():
        # 统一输出编码，避免 GBK 控制台下 emoji/中文报错（盒子环境已设 PYTHONUTF8=1）
        for _s in (sys.stdout, sys.stderr):
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass

    @staticmethod
    def _dw(text):
        """近似显示宽度：CJK/全角/emoji 计 2，其余计 1（横幅自适应宽度用）。"""
        return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)

    @staticmethod
    def _version():
        try:
            for line in (BASE_DIR / "bm-scripts-box-rc.toml").read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("version"):
                    return line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
        return ""

    @staticmethod
    def _title():
        v = Cli._version()
        return f"🎬 视频格式转换{(' v' + v) if v else ''} · 基于 FFmpeg 批量互转"

    @staticmethod
    def _banner(text):
        w = Cli._dw(text) + 4
        bar = "─" * w
        print("┌" + bar + "┐")
        print("│  " + text + "  │")
        print("└" + bar + "┘")

    @staticmethod
    def _section(title):
        print(f"── {title} " + "─" * 22)

    @staticmethod
    def get_path(param_path):
        """解析盒子传入的 JSON 参数文件，返回存在的视频路径列表。"""
        if not (param_path and Path(param_path).exists()):
            return []
        try:
            with open(param_path, "r", encoding="utf-8") as f:
                params = json.load(f)
        except (OSError, json.JSONDecodeError):
            return []
        raw = params.get("data", {}).get("target_paths", [])
        return [p for p in raw if Path(p).exists()]

    def _config_summary(self, config):
        """转换参数摘要（配置分节说明用，两行）。"""
        vc = {"auto": "自动", "libx264": "H.264", "libx265": "H.265", "libvpx-vp9": "VP9", "mpeg4": "MPEG-4"}
        res = {"original": "原始", "480p": "480p", "720p": "720p", "1080p": "1080p"}
        q = {"best": "最佳", "high": "高", "standard": "标准", "low": "较低", "small": "最小"}
        fps_map = {"original": "原始", "24": "24", "25": "25", "30": "30", "50": "50", "60": "60"}
        audio = {"copy": "保留原音轨", "encode": "重新编码", "none": "移除音轨"}
        line1 = (f"🎞️ 输出 {config.get('format')} · 编码 {vc.get(config.get('video_codec'), config.get('video_codec'))}"
                 f" · 分辨率 {res.get(config.get('resolution'), config.get('resolution'))}"
                 f" · 画质 {q.get(config.get('quality'), config.get('quality'))}"
                 f" · 帧率 {fps_map.get(config.get('fps'), config.get('fps'))}"
                 f" · 音频 {audio.get(config.get('audio_mode'), config.get('audio_mode'))}")
        out = config.get("output_dir") or "源文件所在目录"
        mode = "允许覆盖" if config.get("overwrite") else "跳过已存在文件"
        return f"{line1}\n  📁 输出目录 {out} · {mode}"

    def run(self, paths):
        """批处理主流程：扫描 → 配置 → 处理 → 结果 → 倒计时退出。"""
        Cli._banner(Cli._title())

        videos, skipped = [], []
        for p in paths:
            if Path(p).suffix.lower() in VIDEO_EXTS:
                videos.append(p)
            else:
                skipped.append(p)
        if skipped:
            self._section("扫描")
            for p in skipped:
                print(f"  ⏭️ 忽略非视频: {Path(p).name}")

        if not videos:
            print("  ❌ 未选择有效的视频文件")
            self._exit()
            return

        self._section("配置")
        config = Gui.load_config()
        if config is None:
            print("  📋 首次使用，请配置转换参数...")
            config = Gui().ask()
            if config is None:
                print("  ❌ 未获取到配置，已取消转换")
                self._exit()
                return
            Gui.save_config(config)
            print("  ✅ 配置已保存")
        else:
            print("  💾 使用已保存的配置")
        print(f"  {self._config_summary(config)}")

        self._section("处理")
        total = len(videos)
        started = [0]

        def on_start(path):
            started[0] += 1
            print(f"  ▶ ({started[0]}/{total}) 正在转换: {Path(path).name}")

        def on_progress(pct, time_str):
            if pct is not None:
                print(f"\r    进度: {pct:5.1f}%  已编码 {time_str}", end="", flush=True)
            else:
                print(f"\r    进度: ...  已编码 {time_str}", end="", flush=True)

        def on_done(path, status, info):
            print("\r" + " " * 60, end="\r")
            name = Path(path).name
            if status == "success":
                size = VideoConverter.get_file_size(info)
                print(f"  ✅ {name} → {Path(info).name}（{size}）")
            elif status == "skipped":
                print(f"  ⏭️ {name}  {info}")
            else:
                print(f"  ❌ {name}  {(info or '未知错误').strip().splitlines()[0]}")

        converter = VideoConverter(videos, config)
        result = converter.convert(on_start=on_start, on_progress=on_progress, on_done=on_done)

        self._section("结果")
        parts = [f"✅ 成功 {len(result['success'])} 个"]
        if result["skipped"]:
            parts.append(f"⏭️ 跳过 {len(result['skipped'])} 个")
        if result["failed"]:
            parts.append(f"❌ 失败 {len(result['failed'])} 个")
        print("  " + " · ".join(parts))
        self._exit()

    @staticmethod
    def _exit():
        width, total = 10, 5
        for i in range(total, 0, -1):
            filled = round(width * (total - i + 1) / total)
            bar = "█" * filled + "░" * (width - filled)
            print(f"\r  ⏳ {i}s {bar}  按任意键立即退出", end="")
            time.sleep(1)
        print("\r" + " " * 60, end="\r")
        print("  👋 已退出")
        sys.exit(0)


def main():
    Cli._fix_encoding()                      # 先修编码，再打印任何东西
    param_path = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        if param_path:                        # 盒子传入 JSON 参数 → 批处理
            paths = Cli.get_path(param_path)
            if not paths:
                print("未获取到有效的文件路径")
                time.sleep(2)
            else:
                Cli().run(paths)
        else:                                 # 无参 → 打开配置窗口
            Cli._banner(Cli._title())
            config = Gui().ask()
            if config is not None:
                Gui.save_config(config)
            print(("  ✅ 配置已保存" if config else "  未保存配置") + "\n")
            time.sleep(2)
    except FileNotFoundError as e:            # 缺二进制/webview → 中文报错，停留 3 秒
        print(f"❌ {e}")
        time.sleep(3)


if __name__ == "__main__":
    main()
