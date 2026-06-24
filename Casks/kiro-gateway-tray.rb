cask "kiro-gateway-tray" do
  version "0.1.21"

  on_arm do
    sha256 "ea4296d89db289ff6f3a0e38359a13a8226bcccf0298bb825f9581e04823e1c0"
    url "https://github.com/zhujunsan/kiro-gateway-deploy/releases/download/v#{version}/KiroGatewayTray-#{version}-macos-arm64.dmg"
  end
  on_intel do
    sha256 "9c4b11df64946dbd7b9fb46cb90de6ce2e0201f301ac43658c08363012afc561"
    url "https://github.com/zhujunsan/kiro-gateway-deploy/releases/download/v#{version}/KiroGatewayTray-#{version}-macos-amd64.dmg"
  end

  name "Kiro Gateway Tray"
  desc "Cross-platform tray app for kiro-gateway"
  homepage "https://github.com/zhujunsan/kiro-gateway-deploy"

  app "KiroGatewayTray.app"

  postflight do
    ohai "本 App 暂未签名 / 公证。首次打开前请执行一次去隔离命令，否则会被 Gatekeeper 拦："
    puts %Q{    xattr -dr com.apple.quarantine "#{appdir}/KiroGatewayTray.app"}
    puts %Q{    或在「系统设置 → 隐私与安全性」里点「仍要打开」。}
  end
end
