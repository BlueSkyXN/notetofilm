import shutil
import uuid

from fastapi import HTTPException

from .errors import UserFacingError
from .render import digest, media_info, run_process


UPLOAD_TYPES = {'image/png': 'png', 'image/jpeg': 'jpg'}


def validate_and_normalize(task_dir, content, content_type, max_bytes, max_pixels):
    if content_type not in UPLOAD_TYPES:
        raise HTTPException(422, '图片上传仅支持 PNG 或 JPEG')
    if not content or len(content) > max_bytes:
        raise HTTPException(413, f'图片文件必须为 1–{max_bytes} 字节')
    png = content.startswith(b'\x89PNG\r\n\x1a\n')
    jpeg = content.startswith(b'\xff\xd8\xff')
    if (content_type == 'image/png') != png or (png == jpeg):
        raise HTTPException(422, '图片内容与 PNG/JPEG 类型不匹配')

    uploads = task_dir / 'uploads'
    uploads.mkdir(parents=True, exist_ok=True)
    temporary = uploads / f'.{uuid.uuid4().hex}'
    temporary.mkdir()
    try:
        try:
            raw = temporary / ('raw.png' if png else 'raw.jpg')
            raw.write_bytes(content)
            info = media_info(raw)
            stream = next((item for item in info.get('streams', []) if item.get('codec_type') == 'video'), None)
            width = int(stream.get('width') or 0) if stream else 0
            height = int(stream.get('height') or 0) if stream else 0
            if not width or not height or width * height > max_pixels:
                raise HTTPException(422, f'图片尺寸无效或超过 {max_pixels} 像素上限')

            normalized = temporary / ('normalized.png' if png else 'normalized.jpg')
            codec = 'png' if png else 'mjpeg'
            run_process(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(raw), '-map', '0:v:0',
                         '-frames:v', '1', '-c:v', codec, str(normalized)], 30)
            checked = media_info(normalized)
            checked_stream = next((item for item in checked.get('streams', []) if item.get('codec_type') == 'video'), None)
            final_width = int(checked_stream.get('width') or 0) if checked_stream else 0
            final_height = int(checked_stream.get('height') or 0) if checked_stream else 0
            if not final_width or not final_height:
                raise HTTPException(422, '图片规范化后无法读取尺寸，请换用标准 PNG/JPEG')
        except HTTPException:
            raise
        except UserFacingError:
            raise HTTPException(422, '图片无法解码或规范化，请换用标准 PNG/JPEG') from None

        sha = digest(normalized)
        destination = uploads / f'{sha}.{UPLOAD_TYPES[content_type]}'
        if not destination.exists():
            normalized.replace(destination)
        result = {
            'path': str(destination.relative_to(task_dir)), 'sha256': sha,
            'mime': content_type, 'width': final_width, 'height': final_height,
            'byte_size': destination.stat().st_size,
        }
        return result
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
