class WebSocketException(Exception):
    pass


class ConnectionClosed(WebSocketException):
    pass


class InvalidHandshake(WebSocketException):
    pass


class InvalidStatus(InvalidHandshake):
    def __init__(self, response):
        super().__init__("rejected")
        self.response = response


class ProxyError(InvalidHandshake):
    pass


class InvalidProxy(WebSocketException):
    pass
