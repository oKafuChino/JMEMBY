"""One-page image worker. Must never import jmcomic or its HTTP dependencies."""
import errno
import json
from pathlib import Path
import sys
import warnings

from jmemby.domain.errors import JobError
from jmemby.storage.cbz import validate_image, validate_image_file


def decode(request):
    from PIL import Image

    source, destination = Path(request['source']), Path(request['destination'])
    max_bytes, max_pixels = request['max_bytes'], request['max_pixels']
    budget, segments = request['budget'], request['segments']
    if (any(type(n) is not int or n <= 0 for n in (max_bytes, max_pixels))
            or type(budget) is not int or budget < 0
            or type(segments) is not int or not 0 <= segments <= 20):
        raise ValueError('Invalid decoding limits')
    validate_image_file(source, max_bytes)
    previous = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(source) as original:
                if original.format not in {'JPEG', 'PNG', 'WEBP'}:
                    raise JobError('BAD_IMAGE', 'Unsupported image format')
                if original.width * original.height > max_pixels:
                    raise JobError('IMAGE_LIMIT', 'Image exceeds pixel limit')
                if not segments:
                    original.save(destination, 'JPEG')
                else:
                    # JM pages store horizontal strips in reverse order; the
                    # bottom strip also contains height % segments extra rows.
                    width, height = original.size
                    step, remainder = divmod(height, segments)
                    with Image.new('RGB', original.size) as output:
                        bottom, top = height, 0
                        for index in range(segments):
                            rows = step + (remainder if index == 0 else 0)
                            with original.crop((0, bottom - rows, width, bottom)) as strip:
                                output.paste(strip, (0, top))
                            bottom -= rows
                            top += rows
                        output.save(destination, 'JPEG')
            # All source/output pixel buffers are closed before validation.
            if destination.stat().st_size > min(max_bytes, budget):
                raise JobError('SIZE_LIMIT', 'Decoded page exceeds budget')
            validate_image(destination, max_bytes, max_pixels)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        Image.MAX_IMAGE_PIXELS = previous


def main():
    from PIL import Image

    try:
        decode(json.loads(sys.stdin.readline()))
        result = {}
    except Exception as exc:
        if isinstance(exc, JobError):
            code = exc.code
        elif isinstance(exc, MemoryError):
            code = 'MEMORY_LIMIT'
        elif isinstance(exc, (Image.DecompressionBombWarning, Image.DecompressionBombError)):
            code = 'IMAGE_LIMIT'
        elif isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
            code = 'DISK_FULL'
        else:
            code = 'BAD_IMAGE'
        result = {'error': {'code': code}}
    sys.stdout.write(json.dumps(result))


if __name__ == '__main__':
    main()
