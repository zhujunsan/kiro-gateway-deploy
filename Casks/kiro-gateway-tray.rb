cask "kiro-gateway-tray" do
  version "0.5.7"

  on_arm do
    sha256 "8fb82b41f7e60f2993580419b786ea9cceb740506d00b57c200e15ae269dfaf9"
    url "https://github.com/zhujunsan/kiro-gateway-deploy/releases/download/v#{version}/KiroGatewayTray-#{version}-macos-arm64.dmg"
  end
  on_intel do
    sha256 "f5f23f37c4c9464699f764894f2cad3994f9a1a93abb6c87ef27362d1cbbf01e"
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
