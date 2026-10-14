# Optional Caddy Reverse Proxy

SpotifySaver works locally without Caddy. This guide is for deployments that
want to access SpotifySaver over HTTPS through an existing Caddy reverse proxy.

## Before you start

- SpotifySaver is running with Docker Compose.
- You have a hostname whose DNS points to your server.
- Caddy can receive traffic for the hostname. For automatic public HTTPS,
  ports 80 and 443 must reach Caddy.

The default Compose file publishes SpotifySaver only on host loopback at
`127.0.0.1:8000`; do not expose port 8000 directly to the internet.

## Caddy running on the host

Add a site to the Caddyfile managed by your installation:

```caddyfile
spotify.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Replace `spotify.example.com` with your hostname, then reload Caddy using the
method appropriate for your installation. Caddy can obtain and renew HTTPS
certificates automatically when DNS and network access are configured.

## Caddy running in Docker

The Caddy container and SpotifySaver must share a Docker network. The optional
[`docker-compose.caddy.yml`](docker-compose.caddy.yml) override only attaches
SpotifySaver to an existing network; it does not create or configure that
network or change Caddy's configuration.

Set the existing network name in `.env`:

```dotenv
CADDY_DOCKER_NETWORK=caddy
```

Use the actual network name that Caddy is connected to. Then start or update
SpotifySaver with the override:

```sh
docker compose -f docker-compose.yml -f docker-compose.caddy.yml up -d
```

In the Caddyfile, proxy to SpotifySaver by its Compose service name:

```caddyfile
spotify.example.com {
    reverse_proxy spotifysaver:8000
}
```

Configure Caddy to join the same Docker network using the method supported by
your Caddy deployment. The Compose override assumes that the network already
exists and fails if `CADDY_DOCKER_NETWORK` is unset or incorrect.

## Nextcloud All-in-One

Nextcloud AIO's integrated reverse-proxy setup is managed for Nextcloud; do not
assume it is a general-purpose Caddy instance or edit AIO-managed proxy
configuration to add SpotifySaver. If Caddy is a separate external proxy that
you manage and already use with AIO, configure SpotifySaver as another site
using the host or Docker instructions above. For AIO-specific proxy setup, use
the [Nextcloud AIO reverse-proxy documentation](https://github.com/nextcloud/all-in-one/blob/main/reverse-proxy.md).

## Spotify redirect URL

After opening SpotifySaver over HTTPS and signing in, copy the redirect URL
shown in **Settings → Spotify Account** into the Spotify Developer Dashboard.
The URLs must match exactly, including the public hostname and callback path.

Keep the admin password strong and protect the host's configuration directory.
The reverse proxy encrypts traffic; it does not replace SpotifySaver's
authentication.
