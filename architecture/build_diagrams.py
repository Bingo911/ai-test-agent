"""Generate the reviewed architecture's standalone SVG artifacts (stdlib only)."""
from pathlib import Path
from html import escape

ROOT = Path(__file__).resolve().parent
FONT = "'Microsoft YaHei','Noto Sans CJK SC','Segoe UI',sans-serif"
COLORS = {"blue": "#2563eb", "green": "#087f72", "purple": "#7c3aed", "orange": "#c66a15", "gray": "#607086"}


class Diagram:
    def __init__(self, title, subtitle, width=1800, height=1370):
        self.width, self.height = width, height
        self.parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img">',
                      f'<title>{escape(title)}</title><desc>{escape(subtitle)}</desc>',
                      '<defs>' + ''.join(f'<marker id="arr-{k}" markerWidth="10" markerHeight="10" refX="8" refY="5" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,0 L9,5 L0,10 Z" fill="{v}"/></marker>' for k,v in COLORS.items()) + '</defs>',
                      f'<rect width="{width}" height="{height}" fill="#f6f8fc"/>']
        self.text(54, 57, title, 34, "#14213a", "700")
        self.text(56, 94, subtitle, 17, "#596a82")
        self.text(width-54, 56, "AI TEST AGENT  /  V1.1", 16, "#53647d", anchor="end")

    def text(self, x, y, value, size=18, fill="#253651", weight="400", anchor="start"):
        self.parts.append(f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" font-weight="{weight}" fill="{fill}" text-anchor="{anchor}">{escape(value)}</text>')

    def region(self, x, y, w, h, title, color="blue", subtitle=None):
        c = COLORS[color]
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="20" fill="#ffffff" stroke="{c}" stroke-opacity=".22" stroke-width="1.5"/>')
        self.text(x+24, y+34, title, 20, c, "700")
        if subtitle:
            self.text(x+24, y+60, subtitle, 14, "#65748a")

    def box(self, x, y, w, h, title, lines, color="blue"):
        c=COLORS[color]
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="#fdfefe" stroke="{c}" stroke-width="1.6"/>')
        self.parts.append(f'<rect x="{x+1}" y="{y+14}" width="4" height="{h-28}" rx="2" fill="{c}"/>')
        self.text(x+20, y+31, title, 20, "#172a46", "650")
        for i,line in enumerate(lines):
            self.text(x+20, y+58+23*i, line, 15, "#53647a")

    def edge(self, pts, color="blue", dashed=False, label=None, label_xy=None):
        c=COLORS[color]
        path="M " + " L ".join(f"{x},{y}" for x,y in pts)
        dash=' stroke-dasharray="7 5"' if dashed else ''
        self.parts.append(f'<path d="{path}" fill="none" stroke="{c}" stroke-width="2" stroke-linejoin="round"{dash} marker-end="url(#arr-{color})"/>')
        if label and label_xy:
            x,y=label_xy
            # Opaque label backdrop keeps labels readable at connector intersections.
            self.parts.append(f'<rect x="{x-5}" y="{y-15}" width="{sum(14 if ord(t)>127 else 8 for t in label)+10}" height="21" rx="4" fill="#f6f8fc"/>')
            self.text(x,y,label,13,c)

    def note(self, y, title, text):
        self.parts.append(f'<rect x="50" y="{y}" width="1700" height="64" rx="12" fill="#eaf0fa"/>')
        self.text(70,y+26,title,16,"#274a79","700")
        self.text(70,y+49,text,15,"#435b7c")

    def save(self, name):
        self.parts.append('</svg>')
        content='\n'.join(self.parts)
        (ROOT/f'{name}.svg').write_text(content,encoding='utf-8')
        (ROOT/f'{name}.html').write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>'+escape(name)+'</title><style>html,body{margin:0;padding:0;background:#f6f8fc}svg{display:block}</style>'+content+'</html>',encoding='utf-8')


def logical():
    d=Diagram("总体逻辑架构", "用例编译 → 预留调度 → 隔离执行 → 证据与分析；人工协助保持原浏览器会话")
    d.region(50,130,470,1040,"01  控制台与权威数据","blue","REST / SSE · 版本不可变 · 状态持久化")
    d.region(570,130,560,1040,"02  调度与浏览器执行","green","至少一次投递 · 一个执行占一个槽位")
    d.region(1180,130,570,1040,"03  AI 与异步分析","purple","只产生受校验的候选、IR 与分析结论")
    # Primary authoring and control flow.
    d.edge([(285,295),(285,345)])
    d.edge([(285,455),(285,505)])
    d.edge([(285,615),(285,665)])
    d.edge([(480,400),(545,400),(545,270),(605,270)],"green",label="执行申请",label_xy=(482,326))
    d.edge([(845,320),(845,370)],"green")
    d.edge([(845,475),(845,540)],"green",label="预留 ID + 代次",label_xy=(866,513))
    d.edge([(845,665),(845,730)],"green",label="受限私网控制",label_xy=(866,705))
    d.edge([(845,850),(845,900)],"green")
    d.edge([(845,995),(845,1035)],"green")
    # Queue routes to compile and analyze without browser coupling.
    d.edge([(1085,405),(1150,405),(1150,270),(1215,270)],"purple")
    d.edge([(1085,440),(1162,440),(1162,785),(1215,785)],"purple")
    d.edge([(1465,320),(1465,370)],"purple")
    d.edge([(1465,490),(1465,540)],"purple")
    d.edge([(1715,790),(1730,790),(1730,435),(1715,435)],"purple",dashed=True)
    # Human loop and artifact transfer.
    d.edge([(480,897),(545,897),(545,628),(605,628)],"orange",True)
    d.edge([(605,593),(555,593),(555,1067),(480,1067)],"gray",True)
    d.edge([(605,240),(535,240),(535,720),(480,720)],"gray",True)
    d.edge([(1215,1040),(1146,1040),(1146,875),(1105,875),(1105,790),(1085,790)],"orange",True)
    d.box(90,215,390,80,"Web Console",["编辑 / 监控 / 人工处理 / 报告"])
    d.box(90,345,390,110,"入口代理 + FastAPI",["TLS · OIDC / RBAC · 项目权限","REST 请求与 SSE 增量事件"])
    d.box(90,505,390,110,"用例 / 环境 / IR 管理",["Markdown → 修订 → 编译审核","执行绑定固定 IR 与环境快照"])
    d.box(90,665,390,120,"PostgreSQL · 权威状态",["版本 / 执行 / 预留 / 命令 / 事件","Outbox 与业务状态同事务提交"],"blue")
    d.box(90,840,390,115,"Human Task + 操作网关",["单人认领 · 短时票据 · 原会话","秘密输入不落库；恢复由 Worker 验证"],"orange")
    d.box(90,1020,390,105,"对象存储 · 证据",["截图 / DOM / 日志 / 合规 Trace 与视频","授权访问 · 摘要校验 · 生命周期"],"gray")
    d.box(605,215,480,105,"Scheduler / Outbox Dispatcher",["公平排队 → 事务预留 → 执行消息","预留包含池配额、截止时间与代次"],"green")
    d.box(605,370,480,105,"Redis / Celery",["compile / execution / analysis 三类队列","只携带 ID；不是结果权威来源"],"green")
    d.box(605,540,480,125,"Browser Worker · 可信控制进程",["认领预留 / 续租 / 取消 / 步骤状态","Executor + Locator + Result Collector","CSS → Role → Text → XPath → Vision"],"green")
    d.box(605,730,480,120,"每执行独立浏览器容器",["Chrome / Chromium + Playwright 服务","Context / Page 保持至人工处理结束","无 DB / KMS / Redis 长期凭证"],"green")
    d.box(605,900,480,95,"出口代理 / 网络策略",["只允许本执行授权目标及依赖域名"],"orange")
    d.box(605,1035,480,90,"授权测试网站",["Web UI 动作与确定性断言"],"gray")
    d.box(1215,215,500,105,"Compiler Worker",["Markdown Parser → AI 补全 → IR 校验","description-only 转候选后必须审核"],"purple")
    d.box(1215,370,500,120,"AI Provider Adapter / 策略门禁",["脱敏 · 请求 Schema · 限额 · 超时","编译、定位、分析共用受限接口","Worker 的视觉请求也经本门禁"],"purple")
    d.box(1215,540,500,105,"模型提供商（可关闭）",["返回候选和分析；无浏览器控制权","不接收密钥或未净化的原始证据"],"purple")
    d.box(1215,735,500,115,"Failure Analyzer Worker",["读取脱敏证据 → 规则分类 → AI 建议","异步追加报告；不改变执行 outcome"],"purple")
    d.box(1215,950,500,175,"Reconciler + Supervisor",["巡检 QUEUED / 租约 / FINALIZING","撤销旧 epoch · 回收容器 · 幂等收尾","未确认销毁：QUARANTINED 仍占配额","只清理，不续跑剩余测试步骤"],"orange")
    d.note(1200,"读图约定","实线：主请求 / 消息 / 执行方向；虚线：状态访问、证据与人工控制。共享依赖以文字标明，未画所有数据库连线。")
    d.text(56,1311,"关键不变量：不可变 IR  ·  PostgreSQL 为状态来源  ·  单持有者  ·  敏感执行关闭原始 Trace/录像  ·  AI 不判定测试通过",16,"#445b7c")
    d.save('AI-Test-Agent-Architecture')


def deployment():
    d=Diagram("部署架构与信任边界", "生产基线：可信控制面与不可信浏览器隔离；每个执行单独授权、限额和回收",height=1380)
    d.region(50,135,410,1020,"A  用户与访问入口","blue","浏览器端不持有平台秘密")
    d.region(500,135,720,1020,"B  可信控制面","green","私网服务 · 服务身份 · 最小权限")
    d.region(1260,135,490,1020,"C  不可信执行区","orange","每执行独立容器与网络策略")
    d.edge([(255,305),(255,365)])
    d.edge([(415,415),(465,415),(465,270),(535,270)])
    d.edge([(415,425),(485,425),(485,440),(535,440)],"orange",True)
    d.edge([(865,270),(900,270)],"blue")
    d.edge([(690,325),(690,385)],"green")
    d.edge([(690,495),(690,555)],"green")
    d.edge([(865,445),(900,445)],"green")
    d.edge([(690,680),(690,740)],"green")
    d.edge([(1040,495),(1040,525),(780,525),(780,555)],"green")
    d.edge([(865,600),(900,600)],"green")
    d.edge([(1185,610),(1230,610),(1230,390),(1295,390)],"orange",label="创建 / 销毁",label_xy=(1229,535))
    d.edge([(790,680),(790,710),(1245,710),(1245,480),(1295,480)],"green",True,label="私网 mTLS / 会话路由",label_xy=(911,706))
    d.edge([(1505,565),(1505,705)],"orange",label="唯一业务出口",label_xy=(1523,646))
    d.edge([(1505,815),(1505,875)],"orange")
    d.edge([(255,670),(255,735)],"purple")
    d.edge([(535,640),(480,640),(480,619),(415,619)],"purple",True)
    d.box(85,215,330,90,"Web Console",["测试工程师 / 负责人 / 管理员"])
    d.box(85,365,330,115,"Load Balancer / 入口代理",["HTTPS / WSS · 请求体限制","REST / SSE / 人工 WebSocket"])
    d.box(85,565,330,105,"AI 策略与出口",["项目许可 · 脱敏 · 费用预算","仅允许审核后的模型请求"],"purple")
    d.box(85,735,330,110,"模型提供商",["外部或私有化部署，可禁用","不是平台权限或状态来源"],"purple")
    d.box(85,925,330,170,"客户端安全约束",["OIDC issuer + subject 身份键","人工控制票据一次性兑换","OTP / 密码不写控制日志","证据链接按项目权限签发"],"blue")
    d.box(535,215,330,110,"FastAPI × 2+",["业务 API / 鉴权 / SSE","静态页面与敏感证据分源"],"green")
    d.box(900,215,285,110,"身份 / KMS",["OIDC / 版本化密钥","长期凭证仅控制面可用"],"green")
    d.box(535,385,330,110,"调度与控制服务",["Scheduler / Outbox / Reconciler","Human Gateway / 会话路由"],"green")
    d.box(900,385,285,110,"Redis / Celery",["独立工作队列","ID + 预留代次"],"green")
    d.box(535,555,330,125,"Worker 控制进程池",["Compiler / Browser / Analyzer","Browser 槽位持有活会话","可信结果采集与状态写入"],"green")
    d.box(900,555,285,125,"Supervisor",["受限容器生命周期权限","资源 ID + epoch 定位","确认销毁后释放预留"],"green")
    d.box(535,740,650,155,"私有数据服务",["PostgreSQL：RLS / 版本 / 预留 / 事件 / Outbox","对象存储：证据 / 校验 / 加密 / 保留策略","控制面按服务身份访问；浏览器网络无法直接连接","备份与恢复演练；Redis 消息丢失从数据库重建"],"blue")
    d.box(535,955,650,140,"运维与容量",["指标 / 脱敏日志 / 告警 · 不暴露高基数标签","发布先 draining；等待占槽计入容量","异常收尾独立于原 Worker；隔离资源不被误释放"],"gray")
    d.box(1295,215,420,100,"浏览器执行节点池",["每执行独立容器；不能共享宿主网络","非 root / sandbox / CPU / 内存 / PID"],"orange")
    d.box(1295,355,420,210,"执行容器 N",["Playwright Server + Chrome / Chromium","浏览器沙箱 / Context / Page / 临时盘","WAIT_HUMAN 保留原进程和页面","无平台 DB / Redis / KMS 凭证","控制协议只允许已授权控制端连接","容器临时证据仅由控制端受控取回"],"orange")
    d.box(1295,705,420,110,"项目 / 执行出口代理",["域名与依赖白名单 · 阻断元数据地址","DNS / 重定向 / 子资源 / WS 均受约束"],"orange")
    d.box(1295,875,420,100,"授权目标环境",["公网测试站点或获批的项目内网","目标站点不能访问平台控制面"],"gray")
    d.box(1295,1015,420,80,"不允许直接访问 B 区存储",["网络策略 + 无凭证，两层隔离"],"orange")
    d.note(1190,"故障与信任约定","实线：主要访问 / 生命周期操作；虚线：人工或浏览器控制、受限 AI 请求。控制面与执行区隔离必须由生产环境验收。")
    d.text(56,1300,"生产方案是设计目标：单机开发可以模拟拓扑，但不能以同容器进程隔离代替多租户生产安全边界。",17,"#445b7c")
    d.save('AI-Test-Agent-Deployment')


if __name__ == '__main__':
    logical()
    deployment()
    print('Generated two SVG diagrams and standalone HTML previews.')
