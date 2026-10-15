"""ADR 0015: staff forms survive an outage (Q21) without being applied twice."""

from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.http import HttpResponse, HttpResponseRedirect
from django.test import RequestFactory, TestCase

from operations.middleware import FORM_DONE_COOKIE, FormOnceMiddleware
from operations.models import FormSubmission
from operations.tests.test_m02_cases import make_staff

KEY = "0f6c3a52-8c1e-4b9e-9d77-2f0c2b8a1e44"


class FormOnceTests(TestCase):
    def setUp(self):
        self.user = make_staff("form-once")
        self.calls = 0

    def request(self, key=KEY, user=None):
        request = RequestFactory().post("/cases/W69-000001/note/", {"_form_key": key, "t": "x"})
        request.user = user or self.user
        request.session = SessionStore()
        request._messages = FallbackStorage(request)
        return request

    def middleware(self, response):
        def view(request):
            self.calls += 1
            return response

        return FormOnceMiddleware(view)

    def test_same_key_twice_is_applied_once_and_lands_where_the_first_went(self):
        middleware = self.middleware(HttpResponseRedirect("/cases/W69-000001/"))
        first = middleware(self.request())
        self.assertEqual(first.cookies[FORM_DONE_COOKIE].value, KEY)
        again = middleware(self.request())
        self.assertEqual(self.calls, 1)
        self.assertEqual(again.status_code, 302)
        self.assertEqual(again["Location"], "/cases/W69-000001/")

    def test_form_errors_and_failures_can_be_sent_again(self):
        for response in (HttpResponse("form with errors"), HttpResponse(status=500)):
            with self.subTest(status=response.status_code):
                self.middleware(response)(self.request())
                self.assertFalse(FormSubmission.objects.filter(key=KEY).exists())
        self.assertEqual(self.calls, 2)

    def test_without_a_valid_key_or_a_login_nothing_is_recorded(self):
        middleware = self.middleware(HttpResponseRedirect("/"))
        middleware(self.request(key="short"))
        middleware(self.request(user=AnonymousUser()))
        self.assertEqual(self.calls, 2)
        self.assertFalse(FormSubmission.objects.exists())

    def test_keys_are_per_person(self):
        other = make_staff("form-once-2")
        middleware = self.middleware(HttpResponseRedirect("/"))
        middleware(self.request())
        middleware(self.request(user=other))
        self.assertEqual(self.calls, 2)
