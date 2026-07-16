"""The downloadable ticket PDF (orders.pdf.render_order_pdf) -- specifically
that it renders to valid PDF bytes whether or not the org has a logo, since the
logo was added to the PDF header. The QR/layout details are exercised via the
ticket_pdf view tests; this focuses on the logo branch not breaking rendering.
"""

import shutil
import tempfile

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from dashboard.tests import DashFixtureMixin
from orders.pdf import render_order_pdf
from tenants.test_logo import image_bytes


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class TicketPdfLogoTests(DashFixtureMixin, TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(settings.MEDIA_ROOT, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        _event, self.performance, _tier = self.build_ga_event(self.org, self.venue)
        self.order = self.make_paid_order(self.org, self.performance, "20.00", n_tickets=2)

    def test_renders_without_a_logo(self):
        pdf = render_order_pdf(self.order)
        self.assertTrue(pdf.startswith(b"%PDF"))

    def test_renders_with_a_logo(self):
        self.org.logo = SimpleUploadedFile(
            "logo.png", image_bytes(size=(400, 200), mode="RGBA"), content_type="image/png"
        )
        self.org.save()
        self.order.refresh_from_db()
        pdf = render_order_pdf(self.order)
        self.assertTrue(pdf.startswith(b"%PDF"))
        # The embedded logo image makes the branded PDF meaningfully larger.
        self.assertGreater(len(pdf), len(render_order_pdf_without_logo(self.order)))


def render_order_pdf_without_logo(order):
    """Render with the logo temporarily detached, for the size comparison."""
    logo, order.organization.logo = order.organization.logo, None
    try:
        return render_order_pdf(order)
    finally:
        order.organization.logo = logo
