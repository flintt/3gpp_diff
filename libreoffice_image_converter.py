"""Render EMF/WMF files to browser-safe PNG previews with LibreOffice.

This helper intentionally runs in a separate Python process.  The pyuno bridge
loads native LibreOffice libraries, so isolating it keeps a malformed metafile
from taking down the web worker that is parsing the specification.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _property(name, value):
    from com.sun.star.beans import PropertyValue

    item = PropertyValue()
    item.Name = name
    item.Value = value
    return item


def _connect(resolver, pipe_name, process, timeout=20):
    connection = f"uno:pipe,name={pipe_name};urp;StarOffice.ComponentContext"
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"LibreOffice exited before accepting conversions ({process.returncode})"
            )
        try:
            return resolver.resolve(connection)
        except Exception as exc:  # pyuno exposes several connection exceptions.
            last_error = exc
            time.sleep(0.1)
    raise RuntimeError(f"Timed out connecting to LibreOffice: {last_error}")


def _is_usable_png(path):
    try:
        with path.open("rb") as source:
            header = source.read(24)
        return (
            len(header) == 24
            and header.startswith(PNG_SIGNATURE)
            and int.from_bytes(header[16:20], "big") > 0
            and int.from_bytes(header[20:24], "big") > 0
        )
    except OSError:
        return False


def _preview_dpi():
    try:
        requested = int(os.environ.get("VECTOR_PREVIEW_DPI", "200"))
    except ValueError:
        requested = 200
    return min(300, max(96, requested))


def _store_direct_png(provider, graphic, destination):
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.tmp.png"
    )
    try:
        provider.storeGraphic(
            graphic,
            (
                _property("URL", temporary.resolve().as_uri()),
                _property("MimeType", "image/png"),
            ),
        )
        if not _is_usable_png(temporary):
            raise RuntimeError("LibreOffice produced no usable PNG")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _store_high_resolution_png(
    desktop, uno_module, graphic, destination, rasterizer
):
    document = desktop.loadComponentFromURL(
        "private:factory/swriter",
        "_blank",
        0,
        (_property("Hidden", True),),
    )
    if document is None:
        raise RuntimeError("LibreOffice could not create a rendering document")

    token = f"{os.getpid()}.{uuid.uuid4().hex}"
    temporary_pdf = destination.with_name(f".{destination.name}.{token}.pdf")
    raster_prefix = destination.with_name(f".{destination.name}.{token}.raster")
    raster_output = Path(f"{raster_prefix}.png")
    try:
        try:
            page_styles = document.StyleFamilies.getByName("PageStyles")
            style_name = document.CurrentController.ViewCursor.PageStyleName
            page_style = page_styles.getByName(style_name)
            width = graphic.Size100thMM.Width
            height = graphic.Size100thMM.Height
            if width <= 0 or height <= 0:
                raise RuntimeError("metafile has no usable physical dimensions")
            for name, value in (
                ("Width", width),
                ("Height", height),
                ("LeftMargin", 0),
                ("RightMargin", 0),
                ("TopMargin", 0),
                ("BottomMargin", 0),
                ("HeaderIsOn", False),
                ("FooterIsOn", False),
            ):
                setattr(page_style, name, value)

            point = uno_module.createUnoStruct("com.sun.star.awt.Point")
            shape = document.createInstance(
                "com.sun.star.drawing.GraphicObjectShape"
            )
            shape.Position = point
            shape.Size = graphic.Size100thMM
            shape.Graphic = graphic
            document.getDrawPage().add(shape)
            document.storeToURL(
                temporary_pdf.resolve().as_uri(),
                (_property("FilterName", "writer_pdf_Export"),),
            )
        finally:
            document.close(True)

        result = subprocess.run(
            [
                rasterizer,
                "-png",
                "-r",
                str(_preview_dpi()),
                "-singlefile",
                str(temporary_pdf),
                str(raster_prefix),
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
        if result.returncode != 0 or not _is_usable_png(raster_output):
            detail = (result.stderr or result.stdout or result.returncode)
            raise RuntimeError(f"PDF rasterization failed: {detail}")
        os.replace(raster_output, destination)
    finally:
        temporary_pdf.unlink(missing_ok=True)
        raster_output.unlink(missing_ok=True)


def _render(
    context,
    desktop,
    provider,
    service_manager,
    uno_module,
    source,
    destination,
    rasterizer,
):
    stream = service_manager.createInstanceWithContext(
        "com.sun.star.io.SequenceInputStream", context
    )
    stream.initialize((uno_module.ByteSequence(source.read_bytes()),))
    graphic = provider.queryGraphic((_property("InputStream", stream),))
    if graphic is None:
        raise RuntimeError("LibreOffice did not recognize the metafile")

    destination.parent.mkdir(parents=True, exist_ok=True)
    if rasterizer:
        try:
            _store_high_resolution_png(
                desktop, uno_module, graphic, destination, rasterizer
            )
            return
        except Exception:
            # Some malformed metafiles cannot be placed into a temporary
            # Writer page even though LibreOffice can still rasterize them.
            pass
    _store_direct_png(provider, graphic, destination)


def convert(pairs):
    try:
        import uno
    except ImportError as exc:
        raise RuntimeError("the Python UNO bridge is not installed") from exc

    office_binary = shutil.which("soffice") or shutil.which("libreoffice")
    if not office_binary:
        raise RuntimeError("LibreOffice is not installed")

    with tempfile.TemporaryDirectory(prefix="3gpp-lo-profile-") as profile_dir:
        pipe_name = f"gpp_image_{os.getpid()}_{uuid.uuid4().hex}"
        command = [
            office_binary,
            f"-env:UserInstallation={Path(profile_dir).resolve().as_uri()}",
            "--headless",
            "--nologo",
            "--nodefault",
            "--nofirststartwizard",
            "--norestore",
            f"--accept=pipe,name={pipe_name};urp;StarOffice.ComponentContext",
        ]
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        failures = []
        try:
            local_context = uno.getComponentContext()
            resolver = local_context.ServiceManager.createInstanceWithContext(
                "com.sun.star.bridge.UnoUrlResolver", local_context
            )
            context = _connect(resolver, pipe_name, process)
            service_manager = context.ServiceManager
            desktop = service_manager.createInstanceWithContext(
                "com.sun.star.frame.Desktop", context
            )
            provider = service_manager.createInstanceWithContext(
                "com.sun.star.graphic.GraphicProvider", context
            )
            rasterizer = shutil.which("pdftocairo") or shutil.which("pdftoppm")
            for source, destination in pairs:
                try:
                    _render(
                        context,
                        desktop,
                        provider,
                        service_manager,
                        uno,
                        Path(source),
                        Path(destination),
                        rasterizer,
                    )
                except Exception as exc:
                    failures.append(f"{source}: {exc}")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    if failures:
        raise RuntimeError("; ".join(failures))


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or len(arguments) % 2:
        print(
            "usage: libreoffice_image_converter.py INPUT OUTPUT [INPUT OUTPUT ...]",
            file=sys.stderr,
        )
        return 2
    pairs = list(zip(arguments[0::2], arguments[1::2]))
    try:
        convert(pairs)
    except Exception as exc:
        print(f"LibreOffice image conversion failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
