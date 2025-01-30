import datetime
from typing import Any, List, Dict, Optional
from croniter import croniter

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.helper.plugin import PluginBase
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.schemas import NotificationType
from app.schemas.types import EventType


class AutoSpeedLimit(PluginBase):
    # 插件名称
    plugin_name = "下载器自动限速"
    # 插件描述
    plugin_desc = "根据设定的时间段自动调整下载器速度限制。"
    # 插件图标
    plugin_icon = "speedlimit.png"
    # 主题色
    plugin_color = "#ff9800"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "Frank Xu"
    # 作者主页
    author_url = "https://github.com/xuchkang171"
    # 插件配置项ID前缀
    plugin_config_prefix = "autospeedlimit_"
    # 加载顺序
    plugin_order = 21
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _cron = None
    _qb_client = None
    _downloaderhelper = None
    _last_check_time = None
    _speed_rules: List[Dict] = []
    _downloader_name = None

    def init_plugin(self, config: dict = None):
        self._enabled = config.get("enabled")
        if not self._enabled:
            logger.info("插件未启用")
            return
        
        # 初始化下载器助手
        self._downloaderhelper = DownloaderHelper()
        
        # 获取下载器名称
        self._downloader_name = config.get("downloader")
        if not self._downloader_name:
            logger.error("未配置下载器")
            return
            
        # 获取速度规则配置
        self._speed_rules = config.get("speed_rules", [])
        if not self._speed_rules:
            logger.error("未配置速度规则")
            return
        
        logger.info("插件初始化完成")
        logger.info(f"当前下载器: {self._downloader_name}")
        logger.info(f"速度规则: {self._speed_rules}")
            
        # 立即执行一次并设置下次执行时间
        self.check_and_set_speed_limit()

    def get_state(self) -> bool:
        return self._enabled and bool(self._speed_rules)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return [{
            "cmd": "/speedlimit",
            "event": EventType.PluginAction,
            "desc": "查看当前限速状态",
            "category": "下载",
            "data": {
                "action": "speedlimit_state"
            }
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API
        """
        return [{
            "path": "/speedlimit/state",
            "endpoint": self.get_current_state,
            "methods": ["GET"],
            "summary": "获取当前限速状态",
            "description": "获取当前下载器限速状态"
        }]

    def get_form(self) -> List[dict]:
        """
        拼装插件配置页面，需要返回给前端
        """
        # 获取所有qBittorrent下载器
        downloaders = []
        try:
            helper = DownloaderHelper()
            services = helper.get_services(type_filter="qbittorrent")
            for name, service in services.items():
                downloaders.append({
                    "title": name,
                    "value": name
                })
        except Exception as e:
            print(f"获取下载器列表出错: {str(e)}")

        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VSwitch',
                        'field': 'enabled',
                        'name': '启用插件',
                        'value': False
                    },
                    {
                        'component': 'VSelect',
                        'field': 'downloader',
                        'name': '下载器',
                        'options': downloaders,
                        'helper': '选择要自动限速的下载器（目前仅支持qBittorrent）'
                    },
                    {
                        'component': 'VTextarea',
                        'field': 'speed_rules',
                        'name': '速度规则',
                        'value': '[{"cron": "0 8-23 * * *", "upload_limit": 1, "download_limit": 2}]',
                        'helper': 'JSON格式的速度规则列表，每个规则包含：\ncron（何时生效）\nupload_limit（上传限速，MB/s，-1表示不限速）\ndownload_limit（下载限速，MB/s，-1表示不限速）'
                    }
                ]
            }
        ]

    def get_current_state(self) -> dict:
        """
        获取当前限速状态
        """
        if not self._enabled or not self._speed_rules:
            return {"code": 1, "msg": "插件未启用或未配置规则"}
        
        current_rule = self._get_current_rule()
        if not current_rule:
            return {"code": 0, "msg": "当前无生效规则", "data": None}
            
        return {
            "code": 0,
            "msg": "当前限速状态",
            "data": {
                "upload_limit": current_rule.get("upload_limit"),
                "download_limit": current_rule.get("download_limit")
            }
        }

    def _get_next_rule_change_time(self) -> Optional[datetime.datetime]:
        """
        计算下一个规则变化的时间点
        """
        now = datetime.datetime.now()
        next_times = []
        
        # 获取每个规则的下一个触发时间
        for rule in self._speed_rules:
            if not rule.get("cron"):
                continue
            try:
                iter = croniter(rule.get("cron"), now)
                next_time = iter.get_next(datetime.datetime)
                next_times.append(next_time)
                logger.debug(f"【{self.plugin_name}】规则 {rule.get('cron')} 下次触发时间: {next_time}")
            except Exception as e:
                logger.error(f"【{self.plugin_name}】解析cron表达式出错: {str(e)}")
                
        if not next_times:
            return None
            
        # 返回最近的下一个时间点
        next_time = min(next_times)
        logger.info(f"【{self.plugin_name}】下次执行时间: {next_time}")
        return next_time

    def _get_current_rule(self) -> Optional[Dict]:
        """
        获取当前应该生效的规则
        """
        now = datetime.datetime.now()
        matching_rules = []
        
        for rule in self._speed_rules:
            if not rule.get("cron"):
                continue
            try:
                iter = croniter(rule.get("cron"), now)
                prev_dt = iter.get_prev(datetime.datetime)
                next_dt = iter.get_next(datetime.datetime)
                if prev_dt <= now <= next_dt:
                    matching_rules.append((rule, prev_dt))
                    logger.debug(f"【{self.plugin_name}】找到匹配规则: {rule}")
            except Exception as e:
                logger.error(f"【{self.plugin_name}】解析cron表达式出错: {str(e)}")
                
        if not matching_rules:
            logger.info(f"【{self.plugin_name}】当前无匹配规则")
            return None
            
        # 如果多个规则匹配，使用最后生效的规则
        rule = max(matching_rules, key=lambda x: x[1])[0]
        logger.info(f"【{self.plugin_name}】当前使用规则: {rule}")
        return rule

    def check_and_set_speed_limit(self):
        """
        检查并设置速度限制
        """
        if not self._enabled or not self._speed_rules or not self._downloader_name:
            return

        current_rule = self._get_current_rule()
        if not current_rule:
            return

        # 获取下载器实例
        downloader = self._downloaderhelper.get_service(name=self._downloader_name)
        if not downloader or not downloader.instance:
            logger.error(f"【{self.plugin_name}】获取下载器实例失败")
            return
        
        if not self._downloaderhelper.is_downloader(service_type="qbittorrent", service=downloader):
            logger.error(f"【{self.plugin_name}】下载器类型不是qBittorrent")
            return

        try:
            # 转换速度限制从MB/s到KB/s
            upload_limit = current_rule.get("upload_limit", -1)
            download_limit = current_rule.get("download_limit", -1)
            
            # 转换到KB/s，-1保持不变表示不限速
            upload_limit = upload_limit * 1024 if upload_limit >= 0 else -1
            download_limit = download_limit * 1024 if download_limit >= 0 else -1
            
            # 设置限速
            client = downloader.instance
            client.set_speed_limit(
                upload_limit=upload_limit,
                download_limit=download_limit
            )
            
            # 发送通知，显示速度时转换回MB/s
            upload_text = f"{upload_limit/1024:.1f}MB/s" if upload_limit >= 0 else "不限速"
            download_text = f"{download_limit/1024:.1f}MB/s" if download_limit >= 0 else "不限速"
            
            logger.info(f"【{self.plugin_name}】设置限速成功 - 上传: {upload_text}, 下载: {download_text}")
            
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title=f"【{self.plugin_name}】",
                text=f"已设置上传限速: {upload_text}, "
                     f"下载限速: {download_text}"
            )
            
            # 计算并设置下一次检查时间
            next_time = self._get_next_rule_change_time()
            if next_time:
                # 将下一次规则变化的时间转换为cron表达式
                minute = next_time.minute
                hour = next_time.hour
                day = next_time.day
                month = next_time.month
                dow = next_time.weekday()
                self._cron = f"{minute} {hour} {day} {month} {dow}"
                logger.info(f"【{self.plugin_name}】设置下次执行cron: {self._cron}")
                
        except Exception as e:
            logger.error(f"【{self.plugin_name}】设置速度限制出错: {str(e)}")

    @eventmanager.register(EventType.PluginAction)
    def handle_action(self, event: Event):
        """
        处理插件消息
        """
        if not event:
            return
        
        event_data = event.event_data
        if not event_data or event_data.get("action") != "speedlimit_state":
            return
            
        # 获取当前状态
        state = self.get_current_state()
        if state.get("code") == 0 and state.get("data"):
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title="【下载器自动限速】",
                text=f"当前上传限速: {state['data']['upload_limit']}KB/s, "
                     f"下载限速: {state['data']['download_limit']}KB/s"
            )
        else:
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title="【下载器自动限速】",
                text=state.get("msg", "获取状态失败")
            )

    @eventmanager.register(EventType.PluginReload)
    def plugin_reload(self, event: Event):
        """
        插件重载事件
        """
        self.init_plugin(self.get_config())

    @eventmanager.register(EventType.WebhookMessage)
    def webhook(self, event: Event):
        """
        响应WebHook消息
        """
        if not event:
            return
        
        event_data = event.event_data
        if not event_data or event_data.get("type") != "autospeedlimit":
            return
            
        self.check_and_set_speed_limit()

    def stop_service(self):
        """
        退出插件
        """
        pass 