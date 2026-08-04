from django.contrib.auth.models import AnonymousUser
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase


class BaseTemplateVersionTests(SimpleTestCase):
    def test_footer_displays_application_version(self):
        request = RequestFactory().get("/")
        request.user = AnonymousUser()

        rendered = render_to_string("base.html", request=request)

        self.assertIn('<footer class="site-footer">', rendered)
        self.assertIn("Version 2026.08.04", rendered)
