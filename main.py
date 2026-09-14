"""
视频格式转换 - 基于 FFmpeg 的批量格式互转
支持格式: MP4 | MKV | AVI | MOV | WEBM | GIF | M4V | FLV | WMV | TS

界面、定时任务与节点联动均由「不忙脚本盒子」提供：
- 手动触发：盒子依据 TOML 的 params 自动生成表单，用户填写后运行
- 定时任务：invoke_mode == "scheduled"，仅凭 params 无人值守运行
- 节点联动：invoke_mode == "node"，转换完成后把信封写入 output_json
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.request
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

# 默认配置（params 缺省时的兜底，与 TOML 的 default 保持一致）
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
        """同时校验 ffmpeg 与 ffprobe，缺则直接报错（供开局预检）。"""
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
        """顺序转换全部视频，回调供展示；返回分组结果 dict。"""
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


# ==================== 盒子契约辅助 ====================

def _fix_encoding():
    """统一输出编码，避免 GBK 控制台下 emoji/中文报错（盒子环境已设 PYTHONUTF8=1）。"""
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _config_from_params(params: dict) -> dict:
    """从 params 段提取转换配置，缺失项回退默认值。"""
    config = dict(DEFAULT_CONFIG)
    for key in DEFAULT_CONFIG:
        if key in params and params[key] is not None:
            config[key] = params[key]
    config["overwrite"] = bool(config.get("overwrite"))
    return config


def _collect_inputs(data: dict) -> List[str]:
    """汇总输入路径：主数据 target_paths；目录则扫描其下的视频文件（不递归）。去重且只保留存在的文件。"""
    target = data.get("target_paths") or []
    raw = [target] if isinstance(target, str) else list(target)

    paths, seen = [], set()

    def _add(p):
        try:
            key = os.path.normcase(os.path.abspath(p))
        except (OSError, TypeError):
            return
        if key in seen:
            return
        seen.add(key)
        path = Path(p)
        if path.is_file():
            paths.append(str(path))

    for p in raw:
        try:
            key = os.path.normcase(os.path.abspath(p))
        except (OSError, TypeError):
            continue
        if key in seen:
            continue
        seen.add(key)
        path = Path(p)
        if path.is_file():
            paths.append(str(path))
        elif path.is_dir():
            # 选择文件夹：扫描其下的视频文件（不递归）
            for entry in sorted(path.iterdir()):
                if entry.is_file() and entry.suffix.lower() in VIDEO_EXTS:
                    _add(str(entry))
    return paths


def _notify(api_base, notify_type: str, message: str):
    """通过盒子 HTTP API 发送桌面通知（best-effort，失败静默）。"""
    if not api_base or not message:
        return
    try:
        body = json.dumps(
            {"notify_type": notify_type, "message": message}, ensure_ascii=False
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{api_base}/api/notify", data=body,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5).close()
    except Exception:
        pass


def _finish(env: dict, envelope: dict, notify_type: str):
    """收尾：节点/定时任务写回信封，普通触发发送桌面通知。"""
    invoke_mode = env.get("invoke_mode", "manual")

    output_json = env.get("output_json")
    if output_json:
        try:
            with open(output_json, "w", encoding="utf-8") as f:
                json.dump(envelope, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    if invoke_mode != "node":
        _notify(env.get("api_base"), notify_type, envelope.get("summary") or envelope.get("msg"))


def _fail(env: dict, message: str):
    """构造失败信封并收尾。"""
    print(f"❌ {message}")
    envelope = {
        "code": 1, "msg": message, "summary": message,
        "output_paths": [], "skipped_paths": [], "failed_paths": [],
    }
    _finish(env, envelope, "error")


def main():
    _fix_encoding()

    if len(sys.argv) < 2:
        print("请通过「不忙脚本盒子」运行本脚本（未收到参数文件）。")
        return

    try:
        with open(sys.argv[1], "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"❌ 无法读取参数文件：{e}")
        return

    env = payload.get("environment") or {}
    data = payload.get("data") or {}
    params = payload.get("params") or {}

    # 汇总并过滤视频文件
    videos = [p for p in _collect_inputs(data) if Path(p).suffix.lower() in VIDEO_EXTS]
    if not videos:
        _fail(env, "未找到可转换的视频文件")
        return

    config = _config_from_params(params)
    try:
        converter = VideoConverter(videos, config)
    except (FileNotFoundError, ValueError) as e:
        _fail(env, str(e))
        return

    total = len(videos)
    counter = [0]

    def on_start(path):
        counter[0] += 1
        print(f"▶ ({counter[0]}/{total}) 正在转换: {Path(path).name}")

    def on_progress(pct, time_str):
        if pct is not None:
            print(f"\r  进度: {pct:5.1f}%  已编码 {time_str}", end="", flush=True)
        else:
            print(f"\r  进度: ...  已编码 {time_str}", end="", flush=True)

    def on_done(path, status, info):
        print("\r" + " " * 60, end="\r")
        name = Path(path).name
        if status == "success":
            print(f"✅ {name} → {Path(info).name}（{VideoConverter.get_file_size(info)}）")
        elif status == "skipped":
            print(f"⏭️ {name}  {info}")
        else:
            print(f"❌ {name}  {(info or '未知错误').strip().splitlines()[0]}")

    result = converter.convert(on_start=on_start, on_progress=on_progress, on_done=on_done)

    output_paths = [info for _, info in result["success"]]
    skipped_paths = [path for path, _ in result["skipped"]]
    failed_paths = [path for path, _ in result["failed"]]

    parts = [f"成功 {len(result['success'])} 个"]
    if skipped_paths:
        parts.append(f"跳过 {len(skipped_paths)} 个")
    if failed_paths:
        parts.append(f"失败 {len(failed_paths)} 个")
    summary = "转换完成：" + "，".join(parts)

    envelope = {
        "code": 0,
        "msg": "ok",
        "summary": summary,
        "output_paths": output_paths,
        "skipped_paths": skipped_paths,
        "failed_paths": failed_paths,
    }
    print(summary)
    _finish(env, envelope, "success" if result["success"] else "error")


if __name__ == "__main__":
    main()
