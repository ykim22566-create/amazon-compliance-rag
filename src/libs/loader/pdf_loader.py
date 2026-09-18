"""PDF Loader implementation using MarkItDown.

This module implements PDF parsing with image extraction support,
converting PDFs to standardized Markdown format with image placeholders.

Features:
- Text extraction and Markdown conversion via MarkItDown
- Image extraction and storage
- Image placeholder insertion with metadata tracking
- Graceful degradation if image extraction fails
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from markitdown import MarkItDown
    MARKITDOWN_AVAILABLE = True
except ImportError:
    MARKITDOWN_AVAILABLE = False

try:
    import fitz  # PyMuPDF
    PYMUPDF_AVAILABLE = True
except ImportError:
    PYMUPDF_AVAILABLE = False

try:
    import pdfplumber
    PDFPLUMBER_AVAILABLE = True
except ImportError:
    PDFPLUMBER_AVAILABLE = False

try:
    import pypdfium2 as pdfium
    PYPDFIUM_AVAILABLE = True
except ImportError:
    PYPDFIUM_AVAILABLE = False

from PIL import Image
import io

from src.core.types import Document
from src.libs.loader.base_loader import BaseLoader

logger = logging.getLogger(__name__)
logging.getLogger("pdfminer").setLevel(logging.ERROR)


class PdfLoader(BaseLoader):
    """PDF Loader using MarkItDown for text extraction and Markdown conversion.
    
    This loader:
    1. Extracts text from PDF and converts to Markdown
    2. Extracts images and saves to data/images/{doc_hash}/
    3. Inserts image placeholders in the format [IMAGE: {image_id}]
    4. Records image metadata in Document.metadata.images
    
    Configuration:
        extract_images: Enable/disable image extraction (default: True)
        image_storage_dir: Base directory for image storage (default: data/images)
    
    Graceful Degradation:
        If image extraction fails, logs warning and continues with text-only parsing.
    """
    
    def __init__(
        self,
        extract_images: bool = True,
        image_storage_dir: str | Path = "data/images"
    ):
        """Initialize PDF Loader.
        
        Args:
            extract_images: Whether to extract images from PDFs.
            image_storage_dir: Base directory for storing extracted images.
        """
        if not MARKITDOWN_AVAILABLE:
            raise ImportError(
                "MarkItDown is required for PdfLoader. "
                "Install with: pip install markitdown"
            )
        
        self.extract_images = extract_images
        self.image_storage_dir = Path(image_storage_dir)
        self._markitdown = MarkItDown()
    
    def load(self, file_path: str | Path) -> Document:
        """Load and parse a PDF file.
        
        Args:
            file_path: Path to the PDF file.
            
        Returns:
            Document with Markdown text and metadata.
            
        Raises:
            FileNotFoundError: If the PDF file doesn't exist.
            ValueError: If the file is not a valid PDF.
            RuntimeError: If parsing fails critically.
        """
        # Validate file
        path = self._validate_file(file_path)
        if path.suffix.lower() != '.pdf':
            raise ValueError(f"File is not a PDF: {path}")
        
        # Compute document hash for unique ID and image directory
        doc_hash = self._compute_file_hash(path)
        doc_id = f"doc_{doc_hash[:16]}"
        
        # Parse PDF with MarkItDown
        try:
            result = self._markitdown.convert(str(path))
            text_content = result.text_content if hasattr(result, 'text_content') else str(result)
        except Exception as e:
            logger.error(f"Failed to parse PDF {path}: {e}")
            raise RuntimeError(f"PDF parsing failed: {e}") from e
        
        # Initialize metadata
        metadata: Dict[str, Any] = {
            "source_path": str(path),
            "doc_type": "pdf",
            "doc_hash": doc_hash,
        }
        
        # Extract title from first heading if available
        title = self._extract_title(text_content)
        if title:
            metadata["title"] = title
        
        # Handle image extraction (with graceful degradation)
        if self.extract_images:
            try:
                text_content, images_metadata = self._extract_and_process_images(
                    path, text_content, doc_hash
                )
                if images_metadata:
                    metadata["images"] = images_metadata
            except Exception as e:
                logger.warning(
                    f"Image extraction failed for {path}, continuing with text-only: {e}"
                )
        
        return Document(
            id=doc_id,
            text=text_content,
            metadata=metadata
        )
    
    def _compute_file_hash(self, file_path: Path) -> str:
        """Compute SHA256 hash of file content.
        
        Args:
            file_path: Path to file.
            
        Returns:
            Hex string of SHA256 hash.
        """
        sha256 = hashlib.sha256()
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                sha256.update(chunk)
        return sha256.hexdigest()
    
    def _extract_title(self, text: str) -> Optional[str]:
        """Extract title from first Markdown heading or first non-empty line.
        
        Args:
            text: Markdown text content.
            
        Returns:
            Title string if found, None otherwise.
        """
        lines = text.split('\n')
        
        # First try to find a markdown heading
        for line in lines[:20]:  # Check first 20 lines
            line = line.strip()
            if line.startswith('# '):
                return line[2:].strip()
        
        # Fallback: use first non-empty line as title
        for line in lines[:10]:
            line = line.strip()
            if line and len(line) > 0:
                return line
        
        return None
    
    def _extract_and_process_images(
        self,
        pdf_path: Path,
        text_content: str,
        doc_hash: str
    ) -> tuple[str, List[Dict[str, Any]]]:
        """Extract images from PDF and insert placeholders.
        
        Uses PyMuPDF to extract images, save them to disk, and insert
        placeholders in the text content.
        
        Args:
            pdf_path: Path to PDF file.
            text_content: Extracted text content.
            doc_hash: Document hash for image directory.
            
        Returns:
            Tuple of (modified_text, images_metadata_list)
        """
        if not self.extract_images:
            logger.debug(f"Image extraction disabled for {pdf_path}")
            return text_content, []

        if PYMUPDF_AVAILABLE:
            try:
                modified_text, images_metadata = self._extract_embedded_images_pymupdf(
                    pdf_path, text_content, doc_hash
                )
                if images_metadata:
                    logger.info(f"Extracted {len(images_metadata)} images from {pdf_path}")
                    return modified_text, images_metadata
                logger.debug(f"No embedded images found in {pdf_path}")
            except Exception as e:
                logger.warning(f"PyMuPDF image extraction failed for {pdf_path}: {e}")
        else:
            logger.warning(
                "PyMuPDF not available, using rendered image fallback for %s",
                pdf_path,
            )

        return self._extract_rendered_pdf_images(pdf_path, text_content, doc_hash)

    def _extract_embedded_images_pymupdf(
        self,
        pdf_path: Path,
        text_content: str,
        doc_hash: str,
    ) -> tuple[str, List[Dict[str, Any]]]:
        """Extract embedded image XObjects with PyMuPDF."""
        images_metadata: List[Dict[str, Any]] = []
        modified_text = text_content

        image_dir = self.image_storage_dir / doc_hash
        image_dir.mkdir(parents=True, exist_ok=True)

        doc = fitz.open(pdf_path)
        try:
            for page_num in range(len(doc)):
                page = doc[page_num]
                image_list = page.get_images(full=True)

                for img_index, img_info in enumerate(image_list):
                    try:
                        xref = img_info[0]
                        base_image = doc.extract_image(xref)
                        image_bytes = base_image["image"]
                        image_ext = base_image["ext"]

                        image_id = self._generate_image_id(doc_hash, page_num + 1, img_index + 1)
                        image_path = image_dir / f"{image_id}.{image_ext}"

                        with open(image_path, "wb") as img_file:
                            img_file.write(image_bytes)

                        try:
                            img = Image.open(io.BytesIO(image_bytes))
                            width, height = img.size
                        except Exception:
                            width, height = 0, 0

                        placeholder = f"[IMAGE: {image_id}]"
                        insert_position = len(modified_text)
                        modified_text += f"\n{placeholder}\n"

                        try:
                            stored_path = image_path.relative_to(Path.cwd())
                        except ValueError:
                            stored_path = image_path.absolute()

                        images_metadata.append({
                            "id": image_id,
                            "path": str(stored_path),
                            "page": page_num + 1,
                            "text_offset": insert_position + 1,
                            "text_length": len(placeholder),
                            "position": {
                                "width": width,
                                "height": height,
                                "page": page_num + 1,
                                "index": img_index,
                                "source": "pymupdf",
                            },
                        })
                        logger.debug(f"Extracted image {image_id} from page {page_num + 1}")
                    except Exception as e:
                        logger.warning(
                            "Failed to extract image %s from page %s: %s",
                            img_index,
                            page_num + 1,
                            e,
                        )
                        continue
        finally:
            doc.close()

        return modified_text, images_metadata
    
    @staticmethod
    def _generate_image_id(doc_hash: str, page: int, sequence: int) -> str:
        """Generate unique image ID.
        
        Args:
            doc_hash: Document hash.
            page: Page number (0-based).
            sequence: Image sequence on page (0-based).
            
        Returns:
            Unique image ID string.
        """
        return f"{doc_hash[:8]}_{page}_{sequence}"

    def _extract_rendered_pdf_images(
        self,
        pdf_path: Path,
        text_content: str,
        doc_hash: str,
    ) -> tuple[str, List[Dict[str, Any]]]:
        """Fallback extraction for image objects when PyMuPDF is unavailable.

        Some exported PDFs store important diagrams as image XObjects that can
        be located by pdfplumber even when PyMuPDF is not installed. This
        fallback renders each page with pypdfium2, crops large image regions,
        and inserts placeholders so the vision captioner can index them.
        """
        if not PDFPLUMBER_AVAILABLE or not PYPDFIUM_AVAILABLE:
            logger.warning(
                "pdfplumber/pypdfium2 not available, skipping rendered image fallback for %s",
                pdf_path,
            )
            return text_content, []

        image_dir = self.image_storage_dir / doc_hash
        image_dir.mkdir(parents=True, exist_ok=True)

        modified_text = text_content
        images_metadata: List[Dict[str, Any]] = []
        scale = 2.0
        min_width_pt = 100
        min_height_pt = 50
        min_area_pt = 8000

        try:
            pdf_doc = pdfium.PdfDocument(str(pdf_path))
            with pdfplumber.open(str(pdf_path)) as plumber_doc:
                page_count = min(len(pdf_doc), len(plumber_doc.pages))
                for page_idx in range(page_count):
                    page = plumber_doc.pages[page_idx]
                    large_images = [
                        img for img in page.images
                        if img.get("width", 0) >= min_width_pt
                        and img.get("height", 0) >= min_height_pt
                        and img.get("width", 0) * img.get("height", 0) >= min_area_pt
                    ]

                    if not large_images:
                        continue

                    rendered = pdf_doc[page_idx].render(scale=scale).to_pil()

                    for img_index, img_info in enumerate(large_images):
                        try:
                            left = max(0, int(float(img_info["x0"]) * scale))
                            top = max(0, int(float(img_info["top"]) * scale))
                            right = min(rendered.width, int(float(img_info["x1"]) * scale))
                            bottom = min(rendered.height, int(float(img_info["bottom"]) * scale))
                            if right <= left or bottom <= top:
                                continue

                            cropped = rendered.crop((left, top, right, bottom))
                            image_id = self._generate_image_id(
                                doc_hash, page_idx + 1, img_index + 1
                            )
                            image_path = image_dir / f"{image_id}.png"
                            cropped.save(image_path, format="PNG")

                            placeholder = f"[IMAGE: {image_id}]"
                            insert_position = len(modified_text)
                            modified_text += f"\n{placeholder}\n"

                            try:
                                stored_path = image_path.relative_to(Path.cwd())
                            except ValueError:
                                stored_path = image_path.absolute()

                            images_metadata.append({
                                "id": image_id,
                                "path": str(stored_path),
                                "page": page_idx + 1,
                                "text_offset": insert_position + 1,
                                "text_length": len(placeholder),
                                "position": {
                                    "width": cropped.width,
                                    "height": cropped.height,
                                    "page": page_idx + 1,
                                    "index": img_index,
                                    "bbox": [left, top, right, bottom],
                                    "source": "rendered_pdf_crop",
                                },
                            })
                        except Exception as e:
                            logger.warning(
                                "Failed to crop image %s from page %s: %s",
                                img_index,
                                page_idx + 1,
                                e,
                            )
        except Exception as e:
            logger.warning("Rendered image fallback failed for %s: %s", pdf_path, e)
            return text_content, []
        finally:
            try:
                pdf_doc.close()
            except Exception:
                pass

        if images_metadata:
            logger.info(
                "Extracted %s rendered image crops from %s",
                len(images_metadata),
                pdf_path,
            )

        return modified_text, images_metadata
