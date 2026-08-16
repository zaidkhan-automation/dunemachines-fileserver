"""
Global request-rate limiter (slowapi). RATE_LIMIT_SEARCH/RATE_LIMIT_UPLOAD
already existed in Settings but were never wired into any actual
enforcement anywhere in the service (security audit finding, 2026-08) —
this module is the enforcement side.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
