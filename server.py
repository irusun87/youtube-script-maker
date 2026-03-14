#!/usr/bin/env python3
"""
영상 대본 생성기 v2 - 유튜브 자막 + Whisper STT 통합 버전
"""

import os
import re
import json
import http.server
import socketserver
import urllib.request
import urllib.error
import subprocess
import tempfile

PORT = int(os.environ.get("PORT", 8765))
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
HTML = open(os.path.join(os.path.dirname(__file__), "index.html"), encoding="utf-8").read()


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/check-api-key":
            has_key = bool(API_KEY)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            import json as _json
            self.wfile.write(_json.dumps({"has_key": has_key}).encode())
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(HTML.encode("utf-8"))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        payload = json.loads(body)
        try:
            if self.path == "/youtube":
                result = handle_youtube(payload)
            elif self.path == "/whisper":
                result = handle_whisper(payload)
            else:
                raise Exception("알 수 없는 경로입니다.")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode("utf-8"))
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def handle_youtube(payload):
    url = payload.get("url", "").strip()
    api_key = payload.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not url:
        raise Exception("유튜브 URL을 입력해주세요.")
    if not api_key:
        raise Exception("API 키가 없습니다.")
    with tempfile.TemporaryDirectory() as tmpdir:
        subtitle_text = extract_youtube_subtitle(url, tmpdir)
        if not subtitle_text:
            raise Exception("자막을 찾을 수 없습니다. 자막이 없는 영상이거나 비공개 영상일 수 있어요.")
    script = call_claude(api_key, subtitle_text, mode="youtube")
    preview = subtitle_text[:500] + "..." if len(subtitle_text) > 500 else subtitle_text
    return {"script": script, "transcript": preview}


def handle_whisper(payload):
    api_key = payload.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    video_data = payload.get("video_data")
    filename = payload.get("filename", "video.mp4")
    if not api_key:
        raise Exception("API 키가 없습니다.")
    if not video_data:
        raise Exception("영상 파일이 없습니다.")
    import base64
    with tempfile.TemporaryDirectory() as tmpdir:
        video_path = os.path.join(tmpdir, filename)
        with open(video_path, "wb") as f:
            f.write(base64.b64decode(video_data))
        audio_path = os.path.join(tmpdir, "audio.mp3")
        subprocess.run([
            "ffmpeg", "-i", video_path,
            "-vn", "-acodec", "mp3", "-ar", "16000", "-ac", "1",
            audio_path, "-y", "-loglevel", "quiet"
        ], check=True)
        transcript = run_whisper(audio_path)
    script = call_claude(api_key, transcript, mode="whisper")
    preview = transcript[:500] + "..." if len(transcript) > 500 else transcript
    return {"script": script, "transcript": preview}


def extract_youtube_subtitle(url, tmpdir):
    out_tmpl = os.path.join(tmpdir, "sub")
    for lang in ["ko", "en"]:
        subprocess.run([
            "python", "-m", "yt_dlp",
            "--write-sub", "--write-auto-sub",
            "--sub-lang", lang,
            "--sub-format", "vtt",
            "--skip-download",
            "--output", out_tmpl,
            url
        ], capture_output=True, text=True)
        for fname in os.listdir(tmpdir):
            if fname.endswith(".vtt"):
                vtt_path = os.path.join(tmpdir, fname)
                return parse_vtt(vtt_path)
    return None


def parse_vtt(vtt_path):
    entries = []
    seen = set()
    current_time = None
    with open(vtt_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("WEBVTT") or line.startswith("Kind:") or line.startswith("Language:"):
                continue
            if "-->" in line:
                start = line.split("-->")[0].strip()
                parts = start.replace(",", ".").split(":")
                try:
                    if len(parts) == 3:
                        h, m, s = int(parts[0]), int(parts[1]), float(parts[2])
                        total = h * 3600 + m * 60 + s
                    else:
                        m, s = int(parts[0]), float(parts[1])
                        total = m * 60 + s
                    mm = int(total // 60)
                    ss = int(total % 60)
                    current_time = f"{mm:02d}:{ss:02d}"
                except Exception:
                    current_time = None
                continue
            line = re.sub(r'<[^>]+>', '', line)
            line = re.sub(r'&amp;', '&', line)
            line = re.sub(r'&lt;', '<', line)
            line = re.sub(r'&gt;', '>', line)
            line = line.strip()
            if line and line not in seen and current_time:
                seen.add(line)
                entries.append(f"[{current_time}] {line}")
    return "\n".join(entries)


def run_whisper(audio_path):
    safe_path = audio_path.replace("\\", "/")
    whisper_script = "\n".join([
        "import whisper",
        "model = whisper.load_model('base')",
        "result = model.transcribe(r'" + safe_path + "', language='ko')",
        "lines = []",
        "for seg in result['segments']:",
        "    mm = int(seg['start'] // 60)",
        "    ss = int(seg['start'] % 60)",
        "    lines.append('[{:02d}:{:02d}] {}'.format(mm, ss, seg['text'].strip()))",
        "print('\\n'.join(lines))",
    ])
    result = subprocess.run(
        ["python", "-c", whisper_script],
        capture_output=True, text=True, timeout=300
    )
    if result.returncode != 0:
        raise Exception(f"Whisper 오류: {result.stderr[:300]}")
    return result.stdout.strip()


def call_claude(api_key, transcript, mode="youtube"):
    if mode == "youtube":
        source_desc = "유튜브 영상의 자막 (타임코드는 원본 영상의 실제 재생 위치입니다)"
    else:
        source_desc = "영상의 음성을 텍스트로 변환한 내용 (타임코드는 원본 영상의 실제 재생 위치입니다)"

    prompt = (
        "당신은 100만 구독자를 보유한 전략적 유튜버이자, 시청 지속 시간을 극대화하는 숏폼 콘텐츠 전문 에디터입니다.\n"
        "단순 요약이 아니라, 원본 영상을 재가공하여 새로운 가치를 창출하는 창작적 편집에 특화되어 있습니다.\n\n"
        "아래는 " + source_desc + "입니다.\n"
        "---\n"
        + transcript +
        "\n---\n\n"
        "위 내용을 바탕으로 아래 지침에 따라 숏폼 하이라이트 대본을 작성해주세요.\n\n"
        "[콘텐츠 구조]\n"
        "- 비선형적 재구성: 시간 순서대로 나열하지 않습니다. [후킹 클립 -> 문제 제기 -> 해결 방법 -> 결론] 순서로 구성합니다.\n"
        "- 강력한 도입부(Hook): 영상 시작은 나레이션이 아닌, 가장 자극적이거나 결과가 돋보이는 핵심 클립을 먼저 배치하여 3초 안에 시청자를 사로잡습니다.\n\n"
        "[자막 및 나레이션 규칙]\n"
        "- 클립 자막: 타임코드와 함께 표시하되, 인물이 실제로 내뱉는 말(대사)만 추출합니다. 타임코드는 원본 영상 그대로 사용하세요.\n"
        "- 나레이션 비중: 영상 클립 70%, 나레이션 30% 비중을 유지합니다.\n"
        "- 나레이션 톤: 항상 존댓말, 신뢰감 있으면서 궁금증 유발하는 어투.\n"
        "- 커뮤니티 어체: 트렌디하고 자연스러운 한국 온라인 말투. (예: 알고 계셨나요?, 이거 모르면 손해입니다)\n\n"
        "[출력 포맷]\n"
        "[타임코드] 인물 대사\n"
        "[나레이션] 클립 바로 뒤에 배치\n\n"
        "[편집 포인트 제안]\n"
        "대본 하단에 시각적 효과(배속 조절, 자막 강조 등)와 썸네일 전략을 추가로 제공합니다."
    )

    request_body = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 3000,
        "messages": [{"role": "user", "content": prompt}]
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=request_body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        raise Exception(f"Claude API 오류 {e.code}: {error_body}")

    return data["content"][0]["text"]


if __name__ == "__main__":
    print("=" * 55)
    print("  영상 대본 생성기 v2 시작!")
    print(f"  브라우저에서 열기: http://localhost:{PORT}")
    print("  - 유튜브 링크로 자막 추출")
    print("  - 영상 파일 업로드 후 Whisper STT")
    print("  종료: Ctrl+C")
    print("=" * 55)
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        httpd.serve_forever()
