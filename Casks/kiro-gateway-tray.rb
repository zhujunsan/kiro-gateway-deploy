cask "kiro-gateway-tray" do
  version "0.4.40"

  on_arm do
    sha256 "517350e4b74890a62ee173d4c93dd8c3fb9ac89586a09813a7b63dc062310e02"
    url "https://github.com/zhujunsan/kiro-gateway-deploy/releases/download/v#{version}/KiroGatewayTray-#{version}-macos-arm64.dmg"
  end
  on_intel do
    sha256 "f645c9cc150afe20f2893d7f0a4a984b9dc7772c652a68f4b224463aa7899d1a"
    url "https://github.com/zhujunsan/kiro-gateway-deploy/releases/download/v#{version}/KiroGatewayTray-#{version}-macos-amd64.dmg"
  end

  name "Kiro Gateway Tray"
  desc "Cross-platform tray app for kiro-gateway"
  homepage "https://github.com/zhujunsan/kiro-gateway-deploy"

  app "KiroGatewayTray.app"

  postflight do
    system_command "/usr/bin/xattr",
                   args: ["-dr", "com.apple.quarantine", "#{appdir}/KiroGatewayTray.app"],
                   sudo: false
  end
end
