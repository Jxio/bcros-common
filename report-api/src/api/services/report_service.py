# Copyright © 2019 Province of British Columbia
#
# Licensed under the Apache License, Version 2.0 (the 'License');
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an 'AS IS' BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Service to  manage report-templates."""

import base64
from io import BytesIO

from dateutil import parser
from flask import current_app, url_for
from jinja2 import Environment, FileSystemLoader, Template
from jinja2.sandbox import SandboxedEnvironment
import pikepdf
import requests
from weasyprint import HTML

from api.services.page_info import get_pdf_page_count
from api.services.chunk_report_service import ChunkReportService
from api.utils.util import TEMPLATE_FOLDER_PATH


def format_datetime(value, format='short'):  # pylint: disable=redefined-builtin
    """Filter to format datetime globally."""
    dt_format = '%m-%d-%Y'
    if format == 'full':
        dt_format = '%m-%d-%Y %I:%M %p'
    elif format == 'short':
        dt_format = '%m-%d-%Y'
    elif format == 'month':
        dt_format = '%B'
    elif format == 'yyyy-mm-dd':
        dt_format = '%Y-%m-%d'
    elif format == 'mmm dd,yyyy':
        dt_format = '%B %e, %Y'
    elif format == 'detail':
        dt_format = '%B %d, %Y at %I:%M %p Pacific Time'
    elif format == 'unix':
        return int(parser.parse(value).timestamp())

    return parser.parse(value).strftime(dt_format)


ENV = Environment(loader=FileSystemLoader('.'), autoescape=True)
ENV.filters['format_datetime'] = format_datetime


class ReportService:
    """Service for all template related operations."""

    @staticmethod
    def _finalize_pdf(
        template_name: str,
        template_args: object,
        html_out: str,
        generate_page_number: bool,
    ) -> bytes:
        """Route to chunk only when statement_report has groupedInvoices; else render directly."""
        is_statement = 'statement_report' in (template_name or '')
        has_grouped_invoices = bool((template_args or {}).get('groupedInvoices'))
        footer_html = None
        if is_statement and has_grouped_invoices:
            return ChunkReportService.create_chunk_report(
                template_name,
                template_args,
                generate_page_number,
            )
        elif is_statement and generate_page_number:
            #footer_template = ENV.get_template(f'{TEMPLATE_FOLDER_PATH}/statement_footer.html')
            #footer_html = footer_template.render(template_args)
            return ReportService._generate_pdf_with_page_numbers(
                template_name, 
                template_args, 
                html_out
            )
        return ReportService.generate_pdf(html_out, generate_page_number, footer_html=footer_html)
    
    @staticmethod
    def generate_pdf(html_out, generate_page_number: bool = False, footer_html: str = None):
        """Generate pdf out of the html using Gotenberg."""
        gotenberg_url = current_app.config.get('GOTENBERG_URL', 'http://localhost:3000')
        endpoint = f"{gotenberg_url}/forms/chromium/convert/html"
        

        files = [("files", ("index.html", html_out, "text/html"))]
        data = {}

        if generate_page_number and footer_html:
            files.append(("files", ("footer.html", footer_html, "text/html")))
            data["footer"] = "footer.html"

        resp = requests.post(
            endpoint,
            files=files,
            data=data,
            timeout=120
        )
        resp.raise_for_status()
        return resp.content

    @staticmethod
    def generate_single_footer_pdf(footer_template, template_args: dict, current_page: int, total_pages: int):
        """Generate PDF with only footer content, empty page content."""
        gotenberg_url = current_app.config.get('GOTENBERG_URL', 'http://localhost:3000')
        endpoint = f"{gotenberg_url}/forms/chromium/convert/html"

        page_args = template_args.copy()
        page_args['current_page'] = current_page
        page_args['total_pages'] = total_pages
        
        footer_html = footer_template.render(page_args)
        
        # Create empty HTML content
        empty_html = """<!DOCTYPE html>"""
        
        files = [
            ("files", ("index.html", empty_html, "text/html")),
            ("files", ("footer.html", footer_html, "text/html"))
        ]
        data = {"footer": "footer.html"}

        resp = requests.post(
            endpoint,
            files=files,
            data=data,
            timeout=120
        )
        resp.raise_for_status()
        return resp.content
      
    @staticmethod
    def _generate_pdf_with_page_numbers(template_name: str, template_args: object, html_out: str) -> bytes:
        """Generate PDF with page numbers by overlaying footer HTML onto each page."""
        # First pass: Generate complete PDF without page numbers
        first_pass_pdf = ReportService.generate_pdf(html_out, False)
        total_pages = get_pdf_page_count(first_pass_pdf)
        
        # Second pass: Generate footer PDFs for each page using the footer.html template
        footer_pdfs = ReportService._generate_footer_pdfs(template_args, total_pages)
        
        # Third pass: Overlay footer PDFs onto main PDF pages
        return ReportService._overlay_footer_pdfs_on_main_pdf(first_pass_pdf, footer_pdfs)

    @staticmethod
    def _generate_footer_pdfs(template_args: object, total_pages: int) -> list:
        """Generate individual footer PDF for each page using footer.html template."""
        footer_template = ENV.get_template(f'{TEMPLATE_FOLDER_PATH}/statement_footer.html')
        footer_pdfs = []
        for i in range(total_pages):
            footer_pdf = ReportService.generate_single_footer_pdf(footer_template, template_args, i + 1, total_pages)
            footer_pdfs.append(footer_pdf)
        return footer_pdfs

    @staticmethod
    def _overlay_footer_pdfs_on_main_pdf(main_pdf_bytes: bytes, footer_pdfs: list) -> bytes:
        """Overlay footer PDFs onto each page of the main PDF."""
        try:
            with pikepdf.Pdf.open(BytesIO(main_pdf_bytes)) as main_pdf:
                result_pdf = pikepdf.Pdf.new()
                
                # Process each page
                for i, main_page in enumerate(main_pdf.pages):
                    result_pdf.pages.append(main_page)
                    new_page = result_pdf.pages[-1]  # Get the newly added page
                    
                    # If we have a footer PDF for this page, overlay it
                    if i < len(footer_pdfs):
                        try:
                            with pikepdf.Pdf.open(BytesIO(footer_pdfs[i])) as footer_pdf:
                                if len(footer_pdf.pages) > 0:
                                    footer_page = footer_pdf.pages[0]

                                    ReportService._overlay_page_content(new_page, footer_page)
                        except Exception as e:
                            current_app.logger.warning(f"Could not overlay footer on page {i+1}: {e}")
                
                # Save result
                output_buffer = BytesIO()
                result_pdf.save(output_buffer)
                return output_buffer.getvalue()
                
        except Exception as e:
            current_app.logger.error(f"Error overlaying footer PDFs: {e}")

            return main_pdf_bytes

    @staticmethod
    def _overlay_page_content(base_page, overlay_page):
        """Overlay content from overlay_page onto base_page using pikepdf."""
        try:
            if hasattr(base_page, 'add_overlay'):
                base_page.add_overlay(overlay_page)
                return
                    
        except Exception as e:
            current_app.logger.warning(f"Could not overlay page content: {e}")
            pass

    @classmethod
    def create_report_from_stored_template(
        cls,
        template_name: str,
        template_args: object,
        generate_page_number: bool = False,
    ):
        """Create a report from a stored template."""
        template = ENV.get_template(f'{TEMPLATE_FOLDER_PATH}/{template_name}.html')
        bc_logo_url = url_for('static', filename='images/bcgov-logo-vert.jpg')
        registries_url = url_for('static', filename='images/reg_logo.png')
        html_out = template.render(
            template_args, bclogoUrl=bc_logo_url, registriesurl=registries_url
        )

        # Finalize via shared helper (chunk when name contains 'statement_report')
        return ReportService._finalize_pdf(
            template_name,
            template_args,
            html_out,
            generate_page_number,
        )

    @classmethod
    def create_report_from_template(cls, template_string: str, template_args: object,
                                    generate_page_number: bool = False):
        """Create a report from a json template."""
        template_decoded = base64.b64decode(template_string).decode('utf-8')
        # Use a sandboxed environment for user-supplied templates
        sandbox_env = SandboxedEnvironment(autoescape=True)
        sandbox_env.filters['format_datetime'] = format_datetime
        template_ = sandbox_env.from_string(template_decoded)
        html_out = template_.render(template_args)

        report_name = (template_args or {}).get('reportName', '')
        return ReportService._finalize_pdf(
            template_name=report_name,
            template_args=template_args,
            html_out=html_out,
            generate_page_number=generate_page_number,
        )
