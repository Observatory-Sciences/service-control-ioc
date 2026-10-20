# Rocky 9 with systemd as PID 1, for local integration of the scdemo stack.
FROM rockylinux/rockylinux:9

# polkit: the tool runs as scdemo and its unit jobs go through a polkit rule.
# gcc gcc-c++ make perl python3.11-devel: PyPI has softioc wheels for x86_64
# only, so on aarch64 uv builds softioc from source.
RUN dnf install -y systemd python3.11 python3.11-pip iproute procps-ng util-linux \
        polkit gcc gcc-c++ make perl python3.11-devel \
    && dnf clean all

# uv from PyPI, pinned to the Mac's version: ghcr.io is unreachable from the
# Rancher Desktop VM in this environment, so COPY --from=ghcr.io/astral-sh/uv fails.
RUN python3.11 -m pip install --no-cache-dir uv==0.13.1

# Container-systemd hygiene: mask units that need real hardware or a console
# (udev, getty, ttys), so boot is quiet and reaches "running", not "degraded".
# Masking, not deleting wants-symlinks, keeps dbus.socket and friends intact.
RUN systemctl mask systemd-udevd.service systemd-udev-trigger.service \
        systemd-udevd-control.socket systemd-udevd-kernel.socket \
        getty.target console-getty.service getty@tty1.service \
        systemd-vconsole-setup.service

STOPSIGNAL SIGRTMIN+3
CMD ["/usr/sbin/init"]
