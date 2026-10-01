# Reaching Talos from other devices (Tailscale)

The server listens on 127.0.0.1:7420 only. `tailscale serve` publishes it to the tailnet,
over HTTPS with Tailscale's certificate, at:

    https://<machine>.<tailnet>.ts.net:8443   (yours is in TALOS_HOME/web.json)

Talos uses port 8443, so port 443 stays free for anything else on the Mac.

Two checks guard it, both set in `TALOS_HOME/web.json`:

- **The host name:** `tailnet_hosts` is added to the host allow-list, so the tailnet name is served
  and any other name is still refused.
- **The user:** `tailnet_users` lists who may use it. `tailscale serve` sets `Tailscale-User-Login`
  to the signed-in user of the calling device, and a request under the tailnet name without an
  allowed login gets 403.

It is never public: this is `serve`, not `funnel`.

    tailscale serve --bg --https=8443 http://127.0.0.1:7420     # on (persists across restarts)
    tailscale serve --https=8443 off                            # off
    tailscale serve status

(The CLI is `/Applications/Tailscale.app/Contents/MacOS/Tailscale` on a Mac.)
