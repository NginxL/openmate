class RequestException(IOError):
    def __init__(self, *args, response=None):
        super().__init__(*args)
        self.response = response


class HTTPError(RequestException):
    pass


class ConnectionError(RequestException):
    pass


class ProxyError(ConnectionError):
    pass


class SSLError(ConnectionError):
    pass


class Timeout(RequestException):
    pass


class ChunkedEncodingError(RequestException):
    """The body broke off midway (real requests: the connection dropped while reading it)."""


class ConnectTimeout(ConnectionError, Timeout):
    pass


class ReadTimeout(Timeout):
    pass


class InvalidSchema(RequestException, ValueError):
    pass


class InvalidURL(RequestException, ValueError):
    pass


class MissingSchema(RequestException, ValueError):
    pass


class NewConnectionError(Exception):
    """urllib3's: the connection was never made."""


class MaxRetryError(Exception):
    def __init__(self, reason):
        super().__init__("max retries")
        self.reason = reason


class Response:
    def __init__(self, status_code, content=b""):
        self.status_code = status_code
        self.content = content
