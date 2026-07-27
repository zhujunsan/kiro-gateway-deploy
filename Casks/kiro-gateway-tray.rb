cask "kiro-gateway-tray" do
  version "0.4.19"

  on_arm do
    sha256 "a2f520421bce85e5a596604c331fa753488de49cee411d4ff808231525c8e4df"
    url "https://github.com/zhujunsan/kiro-gateway-deploy/releases/download/v#{version}/KiroGatewayTray-#{version}-macos-arm64.dmg"
  end
  on_intel do
    sha256 "da7b64ccca0dd5b9afca67ad1cfed65c7d5ab5017e2def1e390d87e714b1477c"
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
