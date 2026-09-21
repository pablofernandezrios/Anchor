# Skeleton. Milestone 9 completes the build and the COPR publication.
%global forgeurl https://github.com/pablofernandezrios/anchor

Name:           anchor
Version:        0.1.0
Release:        1%{?dist}
Summary:        Focus tool that blocks websites and applications

License:        MIT
URL:            %{forgeurl}
Source0:        %{forgeurl}/archive/v%{version}/%{name}-%{version}.tar.gz
BuildArch:      noarch

BuildRequires:  python3-devel >= 3.12
BuildRequires:  systemd-rpm-macros
Requires:       python3 >= 3.12
Requires:       nftables
Requires:       systemd
Recommends:     gtk4
Recommends:     libadwaita
# Carries the indicator and the notifications as well as the interface.
Recommends:     python3-gobject

%description
Anchor blocks websites and applications during focus sessions, enforces breaks,
and runs sessions on weekly schedules. A block cannot be undone in a moment of
weakness: leaving early costs time and effort, and every escape is recorded.

Anchor provides friction, not guarantees. Anyone with root on the machine can
defeat it; the goal is that doing so is tedious and leaves a record.

%prep
%autosetup

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files anchor

install -Dpm 0644 packaging/systemd/anchord.service %{buildroot}%{_unitdir}/anchord.service
install -Dpm 0644 packaging/systemd/anchor-blockerd.service %{buildroot}%{_unitdir}/anchor-blockerd.service
install -Dpm 0644 packaging/systemd/anchor-agent.service %{buildroot}%{_userunitdir}/anchor-agent.service

install -Dpm 0644 data/essentials.txt %{buildroot}%{_datadir}/anchor/essentials.txt
install -Dpm 0644 data/doh-endpoints.txt %{buildroot}%{_datadir}/anchor/doh-endpoints.txt
install -Dpm 0644 data/doh-domains.txt %{buildroot}%{_datadir}/anchor/doh-domains.txt
install -Dpm 0644 data/tunnels.txt %{buildroot}%{_datadir}/anchor/tunnels.txt
for category in data/categories/*.toml; do
  install -Dpm 0644 "$category" %{buildroot}%{_datadir}/anchor/categories/"$(basename "$category")"
done

install -Dpm 0644 data/applications/org.anchor.Anchor.desktop \
  %{buildroot}%{_datadir}/applications/org.anchor.Anchor.desktop

# The interface's translations (SPEC 14), compiled without gettext's tools.
python3 tools/po.py compile
for catalogue in build/locale/*/LC_MESSAGES/anchor.mo; do
  language="$(basename "$(dirname "$(dirname "$catalogue")")")"
  install -Dpm 0644 "$catalogue" \
    %{buildroot}%{_datadir}/locale/"$language"/LC_MESSAGES/anchor.mo
done

install -d %{buildroot}%{_sysconfdir}/anchor
# Where the user's own categories go, replacing shipped ones by name (SPEC 12).
install -d %{buildroot}%{_sysconfdir}/anchor/categories
install -d %{buildroot}%{_sharedstatedir}/anchor

%check
# Only the tests that need neither root, systemd nor a display.
%pytest -m "not needs_root and not needs_systemd and not needs_display"

%post
%systemd_post anchord.service anchor-blockerd.service

%preun
# Removal must restore the system, active session or not (SPEC 7.6). The
# runtime drop-ins would otherwise refuse the stop below.
rm -f /run/systemd/system/anchord.service.d/anchor-session.conf
rm -f /run/systemd/system/anchor-blockerd.service.d/anchor-session.conf
systemctl daemon-reload || :
# Undo DNS, firewall rules and browser policies. A failure must not abort the
# removal (SPEC 7.6).
if [ -x %{_bindir}/anchor-blockerd ]; then
  %{_bindir}/anchor-blockerd --restore || :
fi
%systemd_preun anchord.service anchor-blockerd.service

%postun
%systemd_postun_with_restart anchord.service anchor-blockerd.service

%files -f %{pyproject_files}
%license LICENSE
%doc README.md CHANGELOG.md
%{_bindir}/anchor
%{_bindir}/anchord
%{_bindir}/anchor-blockerd
%{_bindir}/anchor-agent
%{_bindir}/anchor-gui
%{_unitdir}/anchord.service
%{_unitdir}/anchor-blockerd.service
%{_userunitdir}/anchor-agent.service
%{_datadir}/anchor/essentials.txt
%{_datadir}/anchor/doh-endpoints.txt
%{_datadir}/anchor/doh-domains.txt
%{_datadir}/anchor/tunnels.txt
%{_datadir}/anchor/categories/*.toml
%{_datadir}/applications/org.anchor.Anchor.desktop
%{_datadir}/locale/*/LC_MESSAGES/anchor.mo
%dir %{_sysconfdir}/anchor
%dir %{_sysconfdir}/anchor/categories
%dir %attr(0750,root,root) %{_sharedstatedir}/anchor

%changelog
* Fri Sep 18 2026 Pablo Fernández Ríos <fernandezriospablo06@gmail.com> - 0.1.0-1
- First packaged release.
