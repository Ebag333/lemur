"""
.. module: service
    :platform: Unix
    :copyright: (c) 2015 by Netflix Inc., see AUTHORS for more
    :license: Apache, see LICENSE for more details.
.. moduleauthor:: Kevin Glisson <kglisson@netflix.com>
"""
import arrow

from sqlalchemy import func, or_
from flask import g, current_app

from lemur import database
from lemur.plugins.base import plugins
from lemur.certificates.models import Certificate

from lemur.destinations.models import Destination
from lemur.notifications.models import Notification
from lemur.authorities.models import Authority

from lemur.roles.models import Role

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def get(cert_id):
    """
    Retrieves certificate by it's ID.

    :param cert_id:
    :return:
    """
    return database.get(Certificate, cert_id)


def get_by_name(name):
    """
    Retrieves certificate by it's Name.

    :param name:
    :return:
    """
    return database.get(Certificate, name, field='name')


def delete(cert_id):
    """
    Delete's a certificate.

    :param cert_id:
    """
    database.delete(get(cert_id))


def get_all_certs():
    """
    Retrieves all certificates within Lemur.

    :return:
    """
    return Certificate.query.all()


def find_duplicates(cert_body):
    """
    Finds certificates that already exist within Lemur. We do this by looking for
    certificate bodies that are the same. This is the most reliable way to determine
    if a certificate is already being tracked by Lemur.

    :param cert_body:
    :return:
    """
    return Certificate.query.filter_by(body=cert_body).all()


def update(cert_id, owner, description, active, destinations, notifications):
    """
    Updates a certificate.

    :param cert_id:
    :param owner:
    :param active:
    :return:
    """
    cert = get(cert_id)
    cert.owner = owner
    cert.active = active
    cert.description = description

    database.update_list(cert, 'notifications', Notification, notifications)
    database.update_list(cert, 'destinations', Destination, destinations)

    return database.update(cert)


def mint(issuer_options):
    """
    Minting is slightly different for each authority.
    Support for multiple authorities is handled by individual plugins.

    :param issuer_options:
    """
    authority = issuer_options['authority']

    issuer = plugins.get(authority.plugin_name)

    csr, private_key = create_csr(issuer_options)

    issuer_options['creator'] = g.user.email
    cert_body, cert_chain = issuer.create_certificate(csr, issuer_options)

    cert = Certificate(cert_body, private_key, cert_chain)

    cert.user = g.user
    cert.authority = authority
    database.update(cert)
    return cert, private_key, cert_chain,


def import_certificate(**kwargs):
    """
    Uploads already minted certificates and pulls the required information into Lemur.

    This is to be used for certificates that are created outside of Lemur but
    should still be tracked.

    Internally this is used to bootstrap Lemur with external
    certificates, and used when certificates are 'discovered' through various discovery
    techniques. was still in aws.

    :param kwargs:
    """
    from lemur.users import service as user_service
    cert = Certificate(kwargs['public_certificate'])
    cert.owner = kwargs.get('owner', current_app.config.get('LEMUR_SECURITY_TEAM_EMAIL'))
    cert.creator = kwargs.get('creator', user_service.get_by_email('lemur@nobody'))

    # NOTE existing certs may not follow our naming standard we will
    # overwrite the generated name with the actual cert name
    if kwargs.get('name'):
        cert.name = kwargs.get('name')

    if kwargs.get('user'):
        cert.user = kwargs.get('user')

    database.update_list(cert, 'notifications', Notification, kwargs.get('notifications'))

    cert = database.create(cert)
    return cert


def upload(**kwargs):
    """
    Allows for pre-made certificates to be imported into Lemur.
    """
    cert = Certificate(
        kwargs.get('public_cert'),
        kwargs.get('private_key'),
        kwargs.get('intermediate_cert'),
    )

    database.update_list(cert, 'destinations', Destination, kwargs.get('destinations'))
    database.update_list(cert, 'notifications', Notification, kwargs.get('notifications'))

    cert.owner = kwargs['owner']
    cert = database.create(cert)
    g.user.certificates.append(cert)
    return cert


def create(**kwargs):
    """
    Creates a new certificate.
    """
    cert, private_key, cert_chain = mint(kwargs)

    cert.owner = kwargs['owner']

    database.update_list(cert, 'destinations', Destination, kwargs.get('destinations'))

    database.create(cert)
    cert.description = kwargs['description']
    g.user.certificates.append(cert)
    database.update(g.user)

    # do this after the certificate has already been created because if it fails to upload to the third party
    # we do not want to lose the certificate information.
    database.update_list(cert, 'notifications', Notification, kwargs.get('notifications'))
    database.update(cert)
    return cert


def render(args):
    """
    Helper function that allows use to render our REST Api.

    :param args:
    :return:
    """
    query = database.session_query(Certificate)

    time_range = args.pop('time_range')
    destination_id = args.pop('destination_id')
    notification_id = args.pop('notification_id', None)
    show = args.pop('show')
    # owner = args.pop('owner')
    # creator = args.pop('creator')  # TODO we should enabling filtering by owner

    filt = args.pop('filter')

    if filt:
        terms = filt.split(';')
        if 'issuer' in terms:
            # we can't rely on issuer being correct in the cert directly so we combine queries
            sub_query = database.session_query(Authority.id)\
                .filter(Authority.name.ilike('%{0}%'.format(terms[1])))\
                .subquery()

            query = query.filter(
                or_(
                    Certificate.issuer.ilike('%{0}%'.format(terms[1])),
                    Certificate.authority_id.in_(sub_query)
                )
            )
            return database.sort_and_page(query, Certificate, args)

        if 'destination' in terms:
            query = query.filter(Certificate.destinations.any(Destination.id == terms[1]))
        elif 'active' in filt:  # this is really weird but strcmp seems to not work here??
            query = query.filter(Certificate.active == terms[1])
        else:
            query = database.filter(query, Certificate, terms)

    if show:
        sub_query = database.session_query(Role.name).filter(Role.user_id == g.user.id).subquery()
        query = query.filter(
            or_(
                Certificate.user_id == g.user.id,
                Certificate.owner.in_(sub_query)
            )
        )

    if destination_id:
        query = query.filter(Certificate.destinations.any(Destination.id == destination_id))

    if notification_id:
        query = query.filter(Certificate.notifications.any(Notification.id == notification_id))

    if time_range:
        to = arrow.now().replace(weeks=+time_range).format('YYYY-MM-DD')
        now = arrow.now().format('YYYY-MM-DD')
        query = query.filter(Certificate.not_after <= to).filter(Certificate.not_after >= now)

    return database.sort_and_page(query, Certificate, args)


def create_csr(csr_config):
    """
    Given a list of domains create the appropriate csr
    for those domains

    :param csr_config:
    """
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend()
    )

    # TODO When we figure out a better way to validate these options they should be parsed as unicode
    builder = x509.CertificateSigningRequestBuilder()
    builder = builder.subject_name(x509.Name([
        x509.NameAttribute(x509.OID_COMMON_NAME, unicode(csr_config['commonName'])),
        x509.NameAttribute(x509.OID_ORGANIZATION_NAME, unicode(csr_config['organization'])),
        x509.NameAttribute(x509.OID_ORGANIZATIONAL_UNIT_NAME, unicode(csr_config['organizationalUnit'])),
        x509.NameAttribute(x509.OID_COUNTRY_NAME, unicode(csr_config['country'])),
        x509.NameAttribute(x509.OID_STATE_OR_PROVINCE_NAME, unicode(csr_config['state'])),
        x509.NameAttribute(x509.OID_LOCALITY_NAME, unicode(csr_config['location'])),
    ]))

    builder = builder.add_extension(
        x509.BasicConstraints(ca=False, path_length=None), critical=True,
    )

    for k, v in csr_config.get('extensions', {}).items():
        if k == 'subAltNames':
            # map types to their x509 objects
            general_names = []
            for name in v['names']:
                if name['nameType'] == 'DNSName':
                    general_names.append(x509.DNSName(name['value']))

            builder = builder.add_extension(
                x509.SubjectAlternativeName(general_names), critical=True
            )

    # TODO support more CSR options, none of the authority plugins currently support these options
    #    builder.add_extension(
    #        x509.KeyUsage(
    #            digital_signature=digital_signature,
    #            content_commitment=content_commitment,
    #            key_encipherment=key_enipherment,
    #            data_encipherment=data_encipherment,
    #            key_agreement=key_agreement,
    #            key_cert_sign=key_cert_sign,
    #            crl_sign=crl_sign,
    #            encipher_only=enchipher_only,
    #            decipher_only=decipher_only
    #        ), critical=True
    #    )
    #
    #    # we must maintain our own list of OIDs here
    #    builder.add_extension(
    #        x509.ExtendedKeyUsage(
    #            server_authentication=server_authentication,
    #            email=
    #        )
    #    )
    #
    #    builder.add_extension(
    #        x509.AuthorityInformationAccess()
    #    )
    #
    #    builder.add_extension(
    #        x509.AuthorityKeyIdentifier()
    #    )
    #
    #    builder.add_extension(
    #        x509.SubjectKeyIdentifier()
    #    )
    #
    #    builder.add_extension(
    #        x509.CRLDistributionPoints()
    #    )
    #
    #    builder.add_extension(
    #        x509.ObjectIdentifier(oid)
    #    )

    request = builder.sign(
        private_key, hashes.SHA256(), default_backend()
    )

    # serialize our private key and CSR
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,  # would like to use PKCS8 but AWS ELBs don't like it
        encryption_algorithm=serialization.NoEncryption()
    )

    csr = request.public_bytes(
        encoding=serialization.Encoding.PEM
    )

    return csr, pem


def stats(**kwargs):
    """
    Helper that defines some useful statistics about certifications.

    :param kwargs:
    :return:
    """
    query = database.session_query(Certificate)

    if kwargs.get('active') == 'true':
        query = query.filter(Certificate.elb_listeners.any())

    if kwargs.get('destination_id'):
        query = query.filter(Certificate.destinations.any(Destination.id == kwargs.get('destination_id')))

    if kwargs.get('metric') == 'not_after':
        start = arrow.utcnow()
        end = start.replace(weeks=+32)
        items = database.db.session.query(Certificate.issuer, func.count(Certificate.id))\
            .group_by(Certificate.issuer)\
            .filter(Certificate.not_after <= end.format('YYYY-MM-DD')) \
            .filter(Certificate.not_after >= start.format('YYYY-MM-DD')).all()

    else:
        attr = getattr(Certificate, kwargs.get('metric'))
        query = database.db.session.query(attr, func.count(attr))

        # TODO this could be cleaned up
        if kwargs.get('active') == 'true':
            query = query.filter(Certificate.elb_listeners.any())

        items = query.group_by(attr).all()

    keys = []
    values = []
    for key, count in items:
        keys.append(key)
        values.append(count)

    return {'labels': keys, 'values': values}
