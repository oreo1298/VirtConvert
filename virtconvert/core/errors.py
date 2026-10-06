"""Exception hierarchy shared by the core, CLI and GUI."""


class ConvertError(Exception):
    """Base class for everything that can go wrong while inspecting or converting."""


class ImageFormatError(ConvertError):
    """A file is not a disk image, or its headers are damaged."""


class UnsupportedImageError(ConvertError):
    """A valid image that uses a feature VirtConvert&Create cannot read."""


class ParentNotFoundError(ConvertError):
    """The parent of a differencing image (a checkpoint or snapshot) is missing."""


class QemuImgMissingError(ConvertError):
    def __init__(self, message: str | None = None):
        super().__init__(message or (
            "qemu-img was not found. It does the actual conversion; install it with your "
            "package manager (run 'virtconvert doctor' to see the command for your system)."))


class QemuImgError(ConvertError):
    """qemu-img reported an error."""


class VerifyError(ConvertError):
    """The converted image does not match the source."""


class OperationCancelled(ConvertError):
    def __init__(self, message: str = "Cancelled."):
        super().__init__(message)


def os_error_text(exc: OSError) -> str:
    """'No space left on device (/home/me/disk.vdi)': an OSError for people."""
    text = exc.strerror or str(exc)
    if exc.errno == 28:
        text = "Not enough space: " + text[:1].lower() + text[1:]
    return f"{text} ({exc.filename})" if exc.filename else text
