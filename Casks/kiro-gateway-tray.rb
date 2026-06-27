cask "kiro-gateway-tray" do
  version "0.2.11"

  on_arm do
    sha256 "ab9c1d53a85e9dc3fb4462a2703f7bf3d385036c75d180587106943487e0b351"
    url "https://github.com/zhujunsan/kiro-gateway-deploy/releases/download/v#{version}/KiroGatewayTray-#{version}-macos-arm64.dmg"
  end
  on_intel do
    sha256 "6c6f57702124e54712790e8fbf7e275e74325b68de6b6072b3d3733d5a5f4cf2"
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
