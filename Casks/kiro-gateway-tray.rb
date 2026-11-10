cask "kiro-gateway-tray" do
  version "0.5.11"

  on_arm do
    sha256 "8339d0cc6342394605b01a071e31de4254bdb7398bfdf4b54b8fe183ae92407b"
    url "https://github.com/zhujunsan/kiro-gateway-deploy/releases/download/v#{version}/KiroGatewayTray-#{version}-macos-arm64.dmg"
  end
  on_intel do
    sha256 "73967175b398fee966dfa7d8e9b8da7c4a31e817167aea7ef4d94694cbdb01bf"
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
