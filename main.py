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
from concurrent.futures import ThreadPoolExecutor, as_completed

from pathlib import Path
from typing import List

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

# 下拉选项的显示文案（key 与 converter.py 的合法值一致；dict 顺序即下拉顺序）
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

TEMPLATE_PATH = Path(__file__).parent / "config.html"
CONFIG_PATH = Path(__file__).parent / "config.json"

# 模板里这个占位符会被替换为 `const APP_DATA = {...};`
APP_DATA_MARKER = "/*__APP_DATA__*/"

# 统一输出编码，避免 GBK 控制台下 emoji/中文报错（盒子环境已设 PYTHONUTF8=1）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

class VideoConverter:
    """视频格式转换器(基于 FFmpeg)"""

    def __init__(self, videos: List[str], output_dir: str = "", output_format: str = "mp4",
                 video_codec: str = "auto", resolution: str = "original",
                 quality: str = "standard", fps: str = "original",
                 audio_mode: str = "copy", overwrite: bool = False):
        """
        初始化转换器

        Args:
            videos: 视频文件路径列表
            output_dir: 输出目录（为空则保存到源文件目录）
            output_format: 输出格式 (mp4/mkv/avi/mov/webm/gif/m4v/flv/wmv/ts)
            video_codec: 视频编码器 (auto/libx264/libx265/libvpx-vp9/mpeg4)
            resolution: 分辨率 (original/480p/720p/1080p)
            quality: 画质 (best/high/standard/low/small)
            fps: 帧率 (original/24/25/30/50/60)
            audio_mode: 音频处理 (copy/encode/none)
            overwrite: 是否覆盖已存在的文件
        """
        self.videos = videos
        self.output_dir = output_dir or ""
        self.output_format = output_format.strip().lstrip(".").lower()

        if self.output_format not in VIDEO_FORMATS:
            raise ValueError(f"不支持的格式：{self.output_format}，支持：{', '.join(VIDEO_FORMATS)}")

        # 非法参数值回退默认
        self.video_codec = video_codec if video_codec in ALLOWED_VIDEO_CODECS else "auto"
        self.resolution = resolution if resolution in {"original", *RESOLUTIONS} else "original"
        self.quality = quality if quality in ALLOWED_QUALITIES else "standard"
        self.fps = fps if fps in ALLOWED_FPS else "original"
        self.audio_mode = audio_mode if audio_mode in ALLOWED_AUDIO_MODES else "copy"
        self.overwrite = overwrite

        self._ffmpeg = shutil.which("ffmpeg")
        if not self._ffmpeg:
            raise FileNotFoundError("未找到 FFmpeg（ffmpeg 命令），请确认已安装并在环境变量中")
        self._ffprobe = shutil.which("ffprobe")

        # 创建输出目录
        if self.output_dir:
            os.makedirs(self.output_dir, exist_ok=True)

    def _get_audio_codec(self, input_path: str):
        """探测输入文件的第一个音轨编码器，无音轨或探测失败返回 None"""
        if not self._ffprobe:
            return None
        try:
            proc = subprocess.run(
                [self._ffprobe, "-v", "error", "-select_streams", "a:0",
                 "-show_entries", "stream=codec_name",
                 "-of", "default=nw=1:nk=1", input_path],
                capture_output=True, text=True, errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode == 0:
                codec = proc.stdout.strip()
                return codec or None
        except Exception:
            pass
        return None

    def _get_duration(self, input_path: str):
        """探测输入视频总时长（秒），失败返回 None"""
        if not self._ffprobe:
            return None
        try:
            proc = subprocess.run(
                [self._ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", input_path],
                capture_output=True, text=True, errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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
        """生成输出文件路径"""
        stem = Path(input_path).stem
        ext = self.output_format

        if self.output_dir:
            return os.path.join(self.output_dir, f"{stem}.{ext}")
        else:
            # 输出到源文件目录（同格式转换已跳过，不会覆盖原文件）
            parent = Path(input_path).parent
            return os.path.join(parent, f"{stem}.{ext}")

    def _validate(self):
        """编码器与容器兼容性预校验，返回错误信息字符串，None 表示通过"""
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
        """转换单个视频文件，返回 (路径, 状态, 信息)，状态: success/skipped/failed

        Args:
            on_start: 开始编码前的回调，接收 (文件路径)
            on_progress: 编码中的实时进度回调，接收 (百分比, 已编码时间字符串)；百分比未知时为 None
        """
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

    def convert(self, max_workers: int = 2, progress_callback=None, on_start=None, on_progress=None) -> dict:
        """
        批量转换视频

        Args:
            max_workers: 并发线程数（视频编码为 CPU 密集型，建议 1）
            progress_callback: 每个文件完成后的回调，接收 (已完成数, 总数)
            on_start: 每个文件开始编码前的回调，接收 (文件路径)
            on_progress: 每个文件编码中的实时进度回调，接收 (百分比, 已编码时间字符串)

        Returns:
            dict: 转换结果统计
        """
        total = len(self.videos)
        results = {"success": [], "skipped": [], "failed": [], "total": total}

        if total == 0:
            return results

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(self._convert_single, path, on_start, on_progress): path
                       for path in self.videos}

            for idx, future in enumerate(as_completed(futures), 1):
                path, status, info = future.result()
                if status == "success":
                    results["success"].append((path, info))
                elif status == "skipped":
                    results["skipped"].append((path, info))
                else:
                    results["failed"].append((path, info))

                if progress_callback:
                    progress_callback(idx, total)

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


def _render_html(config) -> str:
    """读取 HTML 模板并注入 APP_DATA（常量单一来源在 Python）。"""
    data = {
        "saved": config,
        "DEFAULTS": DEFAULT_CONFIG,
        "VIDEO_FORMATS": VIDEO_FORMATS,
        "VIDEO_CODEC_OPTIONS": VIDEO_CODEC_OPTIONS,
        "RESOLUTION_OPTIONS": RESOLUTION_OPTIONS,
        "QUALITY_OPTIONS": QUALITY_OPTIONS,
        "FPS_OPTIONS": FPS_OPTIONS,
        "AUDIO_MODE_OPTIONS": AUDIO_MODE_OPTIONS,
    }
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE_PATH.read_text(encoding="utf-8")
    return html.replace(APP_DATA_MARKER, f"const APP_DATA = {payload};")


def _validate_saved(data):
    """对页面返回的配置做二次校验（镜像 HTML 里的 JS 规则）。"""
    if not isinstance(data, dict):
        raise ValueError("返回的数据格式无效")

    fmt = str(data.get("format", "") or "").strip().lstrip(".").lower()
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


def _write_config(data) -> Path:
    data["overwrite"] = bool(data.get("overwrite"))
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return CONFIG_PATH


def _spawn_webview(base_cmd, html):
    """先尝试 stdin 管道传入 HTML；失败则回退到临时 HTML 文件。

    注意 webview 的输入优先级：非空 stdin 优先于位置参数。
    """
    common = dict(
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        return subprocess.run([*base_cmd], input=html, **common)
    except (OSError, ValueError):
        fd, path = tempfile.mkstemp(suffix=".html", prefix="video-format-config-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(html)
            return subprocess.run([*base_cmd, path], input="", **common)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


def run_config_window(config) -> bool:
    """打开 HTML 配置窗口。返回 True 表示已保存配置，False 表示取消/出错。"""
    webview = shutil.which("webview-cli") or shutil.which("webview")
    if not webview:
        raise FileNotFoundError(
            "未找到 webview-cli，请确认已安装并加入 PATH\n"
            "https://github.com/just-be-dev/webview-cli"
        )

    html = _render_html(config)
    base_cmd = [webview, "--title", "视频格式转换 - 配置窗口", "--width", "480", "--height", "720"]
    proc = _spawn_webview(base_cmd, html)

    if proc.returncode == 0:
        try:
            data = json.loads(proc.stdout)
        except ValueError as e:
            print(f"配置窗口返回的数据无法解析：{e}")
            return False
        try:
            _validate_saved(data)
        except (ValueError, TypeError) as e:
            print(f"配置校验失败：{e}")
            return False
        _write_config(data)
        return True
    elif proc.returncode == 2:
        return False  # 用户直接关窗 = 取消
    else:
        # reject(1) / 超时(3) / 用法错误(64)
        msg = (proc.stderr or "").strip()
        if msg:
            print(msg)
        return False


def load_config():
    """加载配置文件，不存在则返回默认配置。"""
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                config.update(json.load(f))
        except Exception:
            pass
    return config


def get_path(param_path):
    initial_files = []

    if param_path and Path(param_path).exists():
        with open(param_path, "r", encoding="utf-8") as f:
            params = json.load(f)
        raw = params.get("data", {}).get("target_paths", [])
        initial_files = [p for p in raw if Path(p).exists()]
    return initial_files


def get_config():
    """读取配置文件，不存在则返回默认配置"""
    config_path = Path(__file__).parent / "config.json"

    # 默认配置（单一来源在 scr/gui.py）
    default_config = dict(DEFAULT_CONFIG)

    if not os.path.exists(config_path):
        return default_config

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
            return {**default_config, **config}
    except (json.JSONDecodeError, ValueError) as e:
        print(f"配置文件格式错误：{e}，使用默认配置")
        return default_config


def _format_param(value, mapping=None):
    """格式化参数显示值"""
    if mapping and value in mapping:
        return mapping[value]
    return value


def cli(video_path: list, config: dict):
    vc_map = {"auto": "自动(随格式)", "libx264": "H.264", "libx265": "H.265/HEVC",
              "libvpx-vp9": "VP9", "mpeg4": "MPEG-4"}
    res_map = {"original": "跟随输入", "480p": "480p", "720p": "720p", "1080p": "1080p"}
    q_map = {"best": "最佳", "high": "高", "standard": "标准", "low": "较低", "small": "最小"}
    fps_map = {"original": "跟随输入", "24": "24", "25": "25", "30": "30",
               "50": "50", "60": "60"}
    audio_map = {"copy": "保留原音轨", "encode": "重新编码", "none": "移除音轨"}

    print("-" * 50)
    print('视频格式转换')
    print("-" * 50)
    print(f"输出格式: {config['format']}   视频编码: {_format_param(config['video_codec'], vc_map)}"
          f"   分辨率: {_format_param(config['resolution'], res_map)}")
    print(f"画质: {_format_param(config['quality'], q_map)}   帧率: {_format_param(config['fps'], fps_map)}"
          f"   音频: {_format_param(config['audio_mode'], audio_map)}")
    print(f"输出目录: {config['output_dir'] if config['output_dir'] else '源文件所在目录'}"
          f"   覆盖模式: {'允许覆盖' if config['overwrite'] else '跳过已存在文件'}")
    print("-" * 50)

    converter = VideoConverter(
        videos=video_path,
        output_dir=config["output_dir"],
        output_format=config["format"],
        video_codec=config["video_codec"],
        resolution=config["resolution"],
        quality=config["quality"],
        fps=config["fps"],
        audio_mode=config["audio_mode"],
        overwrite=bool(config["overwrite"]),
    )

    total_files = len(video_path)
    started = [0]

    def on_start(path):
        started[0] += 1
        print(f"\n▶ 正在转换 ({started[0]}/{total_files}): {os.path.basename(path)}")

    def on_progress(pct, time_str):
        if pct is not None:
            print(f"\r   进度: {pct:5.1f}%   已编码 {time_str}", end="", flush=True)
        else:
            print(f"\r   进度: ...   已编码 {time_str}", end="", flush=True)

    # 视频编码为 CPU 密集型任务，顺序转换可吃满单核且进度清晰
    result = converter.convert(max_workers=1, on_start=on_start, on_progress=on_progress)

    print("\n")
    print(f"✅ 成功：{len(result['success'])} 个")
    for path, output in result["success"]:
        print(f"   {os.path.basename(path)} → {os.path.basename(output)}")

    if result["skipped"]:
        print(f"\n⏭️ 跳过：{len(result['skipped'])} 个")
        for path, reason in result["skipped"]:
            print(f"   {os.path.basename(path)}：{reason}")

    if result["failed"]:
        print(f"\n❌ 失败：{len(result['failed'])} 个")
        for path, error in result["failed"]:
            print(f"   {os.path.basename(path)}：{error}")

    # ── 倒计时 + 按键退出 ──
    print("\n" + "-" * 50)
    print("按任意键立即退出，或等待倒计时自动退出")

    # 倒计时
    for i in range(5, 0, -1):
        print(f"\r⏳ {i} 秒后自动退出... (按任意键退出)", end="")
        time.sleep(1)
    print("\r👋 已退出")
    sys.exit(0)


def main():
    param_path = sys.argv[1] if len(sys.argv) > 1 else None
    config = get_config()
    if param_path:
        paths = get_path(param_path)
        cli(paths, config)
    else:
        run_config_window(config)


if __name__ == "__main__":
    main()
