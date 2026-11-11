"""Client for the hosted SWE-DT evaluation API.

from swe_dt import Client

client = Client()  # API key from SWE_DT_API_KEY
verdict = client.evaluate('django__django-11099', patch)
verdict['result']['resolved']
"""

from swe_dt.client import DEFAULT_URL, ApiError, Client

__all__ = ['DEFAULT_URL', 'ApiError', 'Client']
__version__ = '0.1.0'
