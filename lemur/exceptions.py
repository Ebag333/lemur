"""
.. module: lemur.exceptions
    :copyright: (c) 2018 by Netflix Inc., see AUTHORS for more
    :license: Apache, see LICENSE for more details.
"""
from flask import current_app


class LemurException(Exception):
    def __init__(self, *args, **kwargs):
        current_app.logger.exception(self)


class DuplicateError(LemurException):
    def __init__(self, key):
        self.key = key

    def __str__(self):
        return repr("Duplicate found! Could not create: {0}".format(self.key))


class InvalidListener(LemurException):
    def __str__(self):
        return repr(
            "Invalid listener, ensure you select a certificate if you are using a secure protocol"
        )


class InvalidDistribution(LemurException):
    def __init__(self, field):
        self.field = field

    def __str__(self):
        return repr(
            "Invalid distribution {0}, must use IAM certificates".format(self.field)
        )


class TokenExchangeFailed(LemurException):
    def __init__(self, error, description):
        self.error = error
        self.description = description

    def __str__(self):
        return f'Token exchange failed with {self.error}. {self.description}'


class AttrNotFound(LemurException):
    def __init__(self, field):
        self.field = field

    def __str__(self):
        return repr("The field '{0}' is not sortable or filterable".format(self.field))


class InvalidConfiguration(Exception):
    pass


class InvalidAuthority(Exception):
    pass


class UnknownProvider(Exception):
    pass


class IssuerPaymentRequired(LemurException):
    """
    Raised when there are not enough funds to make an order with a paid certificate issuer.

    402 Payment Required https://tools.ietf.org/html/rfc7231#section-6.5.2

    :param message: the error message provided for insufficient funds.
    """
    code = 402

    def __init__(self, message: str):
        self.message = message
        super().__init__()

    def __str__(self):
        return f"{self.code}: Issuer Payment Required. {self.message}"
