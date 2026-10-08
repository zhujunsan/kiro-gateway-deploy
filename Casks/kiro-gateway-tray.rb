cask "kiro-gateway-tray" do
  version "0.4.49"

  on_arm do
    sha256 "db5d83fd2fd2b601bca7168d8728ee9bc352e232d5a6940694595080333f9baa"
    url "https://github.com/zhujunsan/kiro-gateway-deploy/releases/download/v#{version}/KiroGatewayTray-#{version}-macos-arm64.dmg"
  end
  on_intel do
    sha256 "3a3f8651eee5c3530ef3dad0b6156d1e2a7f37c8fc026f98a29104ebc122d1f4"
    url "https://github.com/zhujunsan/kiro-gateway-deploy/releases/download/v#{version}/KiroGatewayTray-#{version}-macos-amd64.dmg"
  end

  name "Kiro Gateway Tray"
  desc "Cross-platform tray app for kiro-gateway"
  homepage "https://github.com/zhujunsan/kiro-gateway-deploy"

  app "KiroGatewayTray.app"

  postflight_steps do
    run "/usr/bin/xattr",
        args: ["-dr", "com.apple.quarantine", "{{appdir}}/KiroGatewayTray.app"]
  end
end
