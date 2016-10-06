#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Certificate management for haproxy load balancing server.

This script is intended to be run as a cron job on a frequent basis.  It scans
the haproxy backend map for active domain names, determines which ones have
valid certificates and requests new certificates for the remaining domains via
Let's Encrypt.  It finds unused certificates in the haproxy configuration,
removes them and disables the associated Let's Encrypt renewal configuration.
"""

import argparse
import collections
import pathlib
import socket
import ssl
import subprocess
import sys

import OpenSSL.crypto


def get_all_domains(config):
    """Get a list of all configured domains from the haproxy backend map."""
    domains = collections.OrderedDict()
    with open(config.haproxy_backend_map) as backend_map:
        for line in backend_map:
            line = line.strip()
            if line and not line.startswith("#"):
                domain, backend = line.split(None, 1)
                domains[domain] = backend
    return domains


def get_ssl_context(config):
    """Return a standard SSL context."""
    if not get_ssl_context.ctx:
        get_ssl_context.ctx = ssl.create_default_context()
        get_ssl_context.ctx.load_verify_locations("/etc/ssl/certs/ca-certificates.crt")
        if config.letsencrypt_use_staging and config.letsencrypt_fake_cert is not None:
            get_ssl_context.ctx.load_verify_locations(config.letsencrypt_fake_cert)
    return get_ssl_context.ctx

get_ssl_context.ctx = None


def has_valid_cert(config, domain, ctx=None):
    """Verify that localhost serves a valid SSL certificate for the given domain."""
    # The easiest and most reliable way to determin whether we have a valid
    # certificate for a given hostname is to connect to haproxy on port 443 and
    # transfer the hostname we want to enquire about in the SNI extension.
    # Python's SSL implementation verifies whether the certificate is valid and
    # matches the hostname in its default configuration.  This way, we see all
    # certificates that are actually served, including any that might not have
    # been retrieved via Let's Encrypt, and get the matching right for wildcard
    # certificates.  This makes sure we only request new certificates when
    # needed.
    if ctx is None:
        ctx = get_ssl_context(config)
    conn = ctx.wrap_socket(socket.socket(socket.AF_INET), server_hostname=domain)
    try:
        conn.connect(("localhost", 443))
    except ssl.SSLError:
        return False
    else:
        return True
    finally:
        conn.close()


def has_valid_dns_record(config, domain):
    """Determine whether the domain name resolves to this server."""
    try:
        domain_ip = socket.gethostbyname(domain)
    except socket.gaierror:
        return False
    return domain_ip == config.server_ip


def get_certless_domains(config, all_domains):
    """Get a list of domain names that need a new certificate."""
    return [
        domain for domain in all_domains
        if has_valid_dns_record(config, domain) and not has_valid_cert(config, domain)
    ]


def request_cert(config, domains):
    """Request a new SSL certificate for the listed domains"""
    command = [
        "letsencrypt", "certonly",
        "--email", config.contact_email,
        "--authenticator", "standalone",
        "--standalone-supported-challenges", "http-01",
        "--http-01-port", "8080",
        "--non-interactive",
        "--agree-tos",
        "--keep",
        "--expand"
    ]
    if config.letsencrypt_use_staging:
        command.append("--staging")
    for domain in domains:
        command += ["-d", domain]
    result = subprocess.run(command)
    # TODO: Log output on error, log success message on success
    return result.returncode


def request_new_certs(config, all_domains):
    """Request new certificates for all domains that need one."""
    certless_domains = get_certless_domains(config, all_domains)
    domains_by_backend = {}
    for domain in certless_domains:
        backend = all_domains[domain]
        domains_by_backend.setdefault(backend, []).append(domain)
    for domains in domains_by_backend.values():
        try:
            request_cert(config, domains)
        except Exception as exc:  # pylint: disable=broad-except
            # TODO: log exception
            pass


def get_dns_names(cert):
    """Retrieve the DNS names for the given certificate."""
    for i in range(cert.get_extension_count()):
        ext = cert.get_extension(i)
        if ext.get_short_name() == b"subjectAltName":
            dns_names = []
            for component in ext._subjectAltNameString().split(", "):  # pylint: disable=protected-access
                name_type, name = component.split(":", 1)
                if name_type == "DNS":
                    dns_names.append(name)
            return dns_names
    for label, value in cert.get_subject().get_components():
        if label == b"CN":
            return [value.decode("utf8")]
    raise ValueError("the certificate does not contain a valid Common Name, "
                     "nor valid Subject Alternative Names")


def remove_cert(cert_path):
    """Delete the certificate pointed to by the path, and deactivate renewal."""
    cert_path.unlink()
    renewal_config = pathlib.Path("/etc/letsencrypt/renewal", cert_path.stem + ".conf")
    if renewal_config.is_file():
        renewal_config.rename(renewal_config.with_suffix(".disabled"))


def clean_up_certs(config, all_domains):
    """Remove old certificate files from /etc/letsencrypt."""
    active_domains = set(all_domains)
    for cert_path in config.haproxy_certs_dir.glob("*.pem"):
        cert = OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_PEM, cert_path.read_bytes())
        try:
            dns_names = set(get_dns_names(cert))
        except ValueError as exc:
            # TODO: log error, no DNS name found
            continue
        if any("*" in name for name in dns_names):
            # Contains a wildcard.  Not from Let's Encrypt, so we don't touch it.
            continue
        if dns_names.isdisjoint(active_domains):
            # Certificate does not serve any active domains, so we can remove it.
            remove_cert(cert_path)


class ArgumentParser(argparse.ArgumentParser):
    """Argument parser with more useful config file syntax."""

    def convert_arg_line_to_args(self, arg_line):
        """Treat each space-separated word as a separate argument."""
        return arg_line.split()


def main(args=sys.argv[1:]):
    """Parse configuration and perform actions."""
    parser = ArgumentParser(fromfile_prefix_chars="@")
    parser.add_argument("--server-ip", required=True)
    parser.add_argument("--haproxy-backend-map", required=True)
    parser.add_argument("--haproxy-certs-dir", required=True, type=pathlib.Path)
    parser.add_argument("--contact-email", required=True)
    parser.add_argument("--letsencrypt-use-staging", action="store_true")
    parser.add_argument("--letsencrypt-fake-cert")
    config = parser.parse_args(args)
    all_domains = get_all_domains(config)
    request_new_certs(config, all_domains)
    clean_up_certs(config, all_domains)


if __name__ == "__main__":
    main()
