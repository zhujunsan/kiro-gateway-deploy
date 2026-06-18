cask "kiro-gateway-tray" do
  version "0.1.15"

  on_arm do
    sha256 "834976b6ff69ef7936d7abbf1bb18942466e0c89dc3074797aa3455f46286edc"
    url "https://github.com/zhujunsan/kiro-gateway-deploy/releases/download/v#{version}/KiroGatewayTray-#{version}-macos-arm64.dmg"
  end
  on_intel do
    sha256 "21cd6ac60c5cde399f1a3c891e0ab2d6a86719a7ed3a406588fe09fd25bf5a07"
    url "https://github.com/zhujunsan/kiro-gateway-deploy/releases/download/v#{version}/KiroGatewayTray-#{version}-macos-amd64.dmg"
  end

  name "Kiro Gateway Tray"
  desc "Cross-platform tray app for kiro-gateway"
  homepage "https://github.com/zhujunsan/kiro-gateway-deploy"

  app "KiroGatewayTray.app"

  postflight do
    ohai "本 App 暂未签名 / 公证。首次打开前请执行一次去隔离命令，否则会被 Gatekeeper 拦："
    puts %Q{    xattr -dr com.apple.quarantine "#{appdir}/KiroGatewayTray.app"}
    puts "或在「系统设置 → 隐私与安全性」里点「仍要打开」。"
  end
end
