cask "kiro-gateway-tray" do
  version "0.1.22"

  on_arm do
    sha256 "d0077c7875e3e0c6007ca2317c1310ada742160171b6225fc1fb11ce716bc637"
    url "https://github.com/zhujunsan/kiro-gateway-deploy/releases/download/v#{version}/KiroGatewayTray-#{version}-macos-arm64.dmg"
  end
  on_intel do
    sha256 "74f2fa76cc679d12c7b5b952d48d38e4e37d564206d0407bf107f825e78e0abc"
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
