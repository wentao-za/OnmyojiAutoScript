# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import time
from cached_property import cached_property
from datetime import datetime

from module.base.timer import Timer
from module.exception import GameStuckError, GameTooManyClickError, TaskEnd
from module.logger import logger
from module.atom.ocr import RuleOcr

from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_main, page_secret_zones, page_shikigami_records, page_battle_result, any_of
from tasks.Secret.config import SecretConfig, Secret
from tasks.Secret.assets import SecretAssets
from tasks.Component.GeneralBattle.general_battle import ExitMatcher, GeneralBattle
from tasks.Component.GeneralBattle.config_general_battle import GeneralBattleConfig
from tasks.Component.SwitchSoul.switch_soul import SwitchSoul
from tasks.Component.GeneralBuff.config_buff import BuffClass


class ShareSecretTimeout(Exception):
    """秘闻分享整段超时（内部信号，在 `share_secret()` 内消化，不外抛）。"""


class ScriptTask(GameUi, GeneralBattle, SwitchSoul, SecretAssets):
    lay_list = ['壹', '贰', '叁', '肆', '伍', '陆', '柒', '捌', '玖', '拾']

    # 秘闻分享整段超时（秒）。正常路径（进秘闻 → 点分享 → 点微信 → 领奖励）
    # 约 20~40 秒，取 120 秒留足转场与网络余量。
    # 超时即放弃分享，不抛异常、不重试：分享只是附赠环节，若放任它触发 device 层
    # GameStuckError，框架会重启游戏并按失败状态重跑整个秘闻副本（script.py
    # _handle_task_exception 对 GameStuckError 的处理就是重启 + 10 秒等待）。
    SHARE_SECRET_TIMEOUT = 120

    def _exit_matcher(self) -> ExitMatcher:
        return self.I_SE_FIRE

    @cached_property
    def match_layer(self) -> dict:
        return {
            '壹': 1, '贰': 2,
            '叁': 3, '肆': 4,
            '伍': 5, '陆': 6,
            '柒': 7, '捌': 8,
            '玖': 9, '拾': 10,
        }

    @cached_property
    def battle_config(self) -> GeneralBattleConfig:
        conf = self.config.model.secret.general_battle
        conf.lock_team_enable = False
        return conf

    def before_run(self):
        battle_result = self.navigator.resolve_page(page_battle_result)
        battle_result.recognizer = any_of(self.I_SE_BATTLE_WIN, battle_result.recognizer)

    def run(self):
        self.before_run()
        self.check_time()
        secret: Secret = self.config.secret
        con = secret.secret_config
        if secret.switch_soul.enable:
            self.goto_page(page_shikigami_records)
            self.run_switch_soul(secret.switch_soul.switch_group_team)
        if secret.switch_soul.enable_switch_by_name:
            self.goto_page(page_shikigami_records)
            self.run_switch_soul_by_name(secret.switch_soul.group_name, secret.switch_soul.team_name)
        self.goto_page(page_secret_zones)

        # 进入
        success = True
        self.ui_click(self.I_SE_ENTER, self.I_SE_FIRE)
        time.sleep(1)  # 有一个很傻逼的动画
        self.screenshot()
        if not self.appear(self.I_SE_PLACEMENT):
            logger.warning('Unsuccessful entry. You must have entered the secret zone before.')
            success = False

        # 开始
        logger.info('Start secret zone')
        first_battle = True
        while 1:
            self.screenshot()
            if not success:
                logger.warning('Secret zone failed to enter, skip')
                break
            if not self.appear(self.I_SE_FIRE):
                continue
            if self.appear(self.I_SE_FINISHED_1):
                logger.info('Secret zone finished')
                break
            layer = self.find_battle()
            logger.info(f'Current layer: {layer}')
            if not layer:
                if self.appear(self.I_SE_SHARE_BUTTON):
                    logger.warning('You have completed the weekly trifles, skip')
                    break
                text = self.O_SE_TOTAL_TIME.ocr_single(self.device.image)
                if '尚未' not in text:
                    logger.warning('You have completed the weekly trifles, skip')
                    break
                continue
            if layer >= 6:
                first_battle = False
            if first_battle and layer <= 5:
                first_battle = False
                buff = []
                if con.secret_gold_50:
                    buff.append(BuffClass.GOLD_50)
                if con.secret_gold_100:
                    buff.append(BuffClass.GOLD_100)
                if buff is []:
                    buff = None
                self.click_battle()
                success = self.run_general_battle(self.battle_config, buff=buff, battle_key="secret")
                continue
            if not first_battle and layer == 6:
                # 第六次关闭加成，但是发现没有这个接口。。。！！！居然没有注意到
                buff = []
                if con.secret_gold_50:
                    buff.append(BuffClass.GOLD_50_CLOSE)
                if con.secret_gold_100:
                    buff.append(BuffClass.GOLD_100_CLOSE)
                if buff is []:
                    buff = None
                self.click_battle()
                success = self.run_general_battle(self.battle_config, buff=buff, battle_key="secret")
                continue
            elif not first_battle and layer == 9 and con.layer_9:
                self.click_battle()
                success = self.run_general_battle(self.battle_config, battle_key="secret")
                continue
            elif not first_battle and layer == 10 and con.layer_10:
                self.click_battle()
                success = self.run_general_battle(self.battle_config, battle_key="secret")
                break
            elif not first_battle:
                # 其他层
                self.click_battle()
                success = self.run_general_battle(self.battle_config, battle_key="secret")
                continue

        # 秘闻分享：接在本周秘闻副本流程之后执行（原先挂在「每周琐事」里）。
        # 放在关加成之前，因为此刻人还停在秘闻页，少一次「秘闻 → 庭院 → 秘闻」往返；
        # 分享失败/超时只记一条错误日志，不改变本任务的收尾结果。
        if con.secret_share:
            self.share_secret()

        if con.secret_gold_50 or con.secret_gold_100:
            self.goto_page(page_main)
            self.open_buff()
            if con.secret_gold_50:
                self.gold_50(False)
            if con.secret_gold_100:
                self.gold_100(False)
            self.close_buff()
        else:
            self.finish_task_navigation()
        self.set_next_run(task='Secret', success=True, finish=True)
        raise TaskEnd('Secret')

    # ------------------------------------------------------------ 秘闻分享

    def share_secret(self) -> bool:
        """秘闻分享（从「每周琐事」迁移而来）：超时即放弃，不抛异常、不重试。

        返回 True 表示分享环节正常走完（含「本周已领」「本周未通关」等正常早退），
        False 表示超时或异常被吞掉。返回值只用于日志，不参与任务收尾判定。

        容错约定：卡住不是错误，是已知的界面不确定性，到点就结束这一环节；只留一条
        error 说明原因，不打异常栈、不重试、不重新拉起游戏。必须吞掉
        `GameStuckError` / `GameTooManyClickError`：这两个异常一旦冒到框架层，
        `script.py::_handle_task_exception` 会重启游戏并按失败状态重跑整个秘闻副本，
        代价远大于放弃一次分享。
        """
        timer = Timer(self.SHARE_SECRET_TIMEOUT).start()
        try:
            self._share_secret_flow(timer)
            return True
        except ShareSecretTimeout:
            logger.error(f'秘闻分享超时：{self.SHARE_SECRET_TIMEOUT} 秒内未完成，跳过本次分享并结束该环节，不重试')
        except (GameStuckError, GameTooManyClickError) as e:
            # device 层在抛出前已清自己的记录，这里再清一次是兜底：收尾还要 goto_page，
            # 残留的 detect/click 记录会让导航立刻二次触发同一个异常。
            self.device.stuck_record_clear()
            self.device.click_record_clear()
            logger.error(f'秘闻分享中断：界面长时间无响应（{type(e).__name__}），跳过本次分享并结束该环节，不重试')
        except Exception as e:
            self.device.stuck_record_clear()
            self.device.click_record_clear()
            logger.error(f'秘闻分享中断：{type(e).__name__}: {e}，跳过本次分享并结束该环节，不重试')
        return False

    def _share_secret_flow(self, timer: Timer) -> None:
        """秘闻分享主流程。超时抛 `ShareSecretTimeout`，其余异常由调用方统一吞掉。

        与「每周琐事」原实现的三处差异：①每个轮询循环顶部检查整段超时，不再用
        无界 `while` 空转到 device 层判死；②不再自己调 `finish_task_navigation()`，
        收尾交给 `run()`，否则会打断后面的关加成；③日志改中文。
        """
        logger.hr('秘闻分享', 2)
        self.goto_page(page_secret_zones)
        # 一路点进秘闻详情：先「进入秘闻」，再「分享」，直到微信分享面板出现
        share_clicked = False
        while 1:
            if timer.reached():
                raise ShareSecretTimeout
            self.screenshot()
            if self.appear(self.I_SE_SHARE_WECHAT):
                self.wait_until_stable(self.I_SE_SHARE_WECHAT, skip_first_screenshot=True)
                break
            if self.appear_then_click(self.I_SE_SHARE_ENTER, interval=1):
                continue
            if self.appear_then_click(self.I_SE_SHARE_BUTTON, interval=5):
                share_clicked = True
                continue
            if self.appear(self.I_SE_SHARE_RANK) and not share_clicked:
                # 出现排名却没有分享按钮：本周没通关，无从分享
                self.wait_until_stable(self.I_SE_SHARE_BUTTON, skip_first_screenshot=True)
                if self.appear(self.I_SE_SHARE_BUTTON):
                    continue
                logger.warning('本周尚未通关秘闻，跳过分享')
                return
        logger.info('已进入秘闻详情')
        # 判本周是否已领过：勾玉图标在＝还没领，不在＝已领
        self.screenshot()
        if not self.appear(self.I_SE_SHARE_JADE):
            logger.info('本周秘闻分享奖励已领取过，跳过')
            return
        self._share_secret_click(timer)

    def _share_secret_click(self, timer: Timer) -> bool:
        """点开分享面板 → 点微信 → 等奖励。返回 False 表示奖励没等到（多为已领过）。"""
        while 1:
            if timer.reached():
                raise ShareSecretTimeout
            self.screenshot()
            if self.appear(self.I_SE_SHARE_QR):
                break
            if self.appear_then_click(self.I_SE_SHARE_WECHAT, interval=2.5):
                continue
        logger.info('分享面板已打开')
        reward_timer = Timer(7).start()
        while 1:
            if timer.reached():
                raise ShareSecretTimeout
            self.screenshot()
            if self.ui_reward_appear_click():
                logger.info('已领取秘闻分享奖励')
                return True
            if self.appear_then_click(self.I_SE_SHARE_QR,
                                      self.C_SE_SHARE_CLICK, interval=4.8):
                continue
            if reward_timer.reached():
                logger.warning('分享奖励未出现，可能本周已领取过')
                return False

    def find_battle(self, screenshot: bool = False) -> int or None:
        """
        自动寻找挑战的层数并且选定 , 找不到会向下划一点
        :return: 如果找得到返回层数，找不到返回None
        """

        def confirm_layer(ocr_target: RuleOcr, roi=None) -> int or None:
            """
            检查层数， 启用函数check_layer
            :param ocr_target:
            :param roi:
            :return:
            """
            ocr_target.roi[0] = int(roi[0]) - 118
            ocr_target.roi[1] = int(roi[1]) + 37
            # print(f'检测到的未通过ROI: {roi}')
            # print(f'检测到的勾玉数量ROI: {ocr_target.roi}')
            jade_num = ocr_target.ocr(self.device.image)
            if isinstance(jade_num, str):
                logger.warning(f'OCR failed, try again {jade_num}')
                return None
            elif not isinstance(jade_num, int):
                logger.warning(f'OCR failed, try again {jade_num}')
                return None
            if jade_num < 7:
                # 第一个的时候可能是没有检测到
                gold_number = self.O_SE_GOLD.ocr(self.device.image)
                if isinstance(gold_number, int) and (gold_number == 10000 or gold_number == 18000):
                    logger.info(f'No find jade number, but find gold number {gold_number}')
                    return 1
                return None
            elif jade_num > 70:
                logger.warning(f'OCR failed, try again {jade_num}')
                return None
            # 勾玉数量 = 层数 * 7
            try:
                lr = jade_num // 7
                return lr
            except TypeError:
                logger.warning(f'OCR failed, try again {jade_num}')
                return None

        if screenshot:
            self.screenshot()
        if self.appear(self.I_CHAT_CLOSE_BUTTON):
            self.ui_click_until_disappear(self.I_CHAT_CLOSE_BUTTON, interval=2)
        text_pos = self.O_SE_NO_PASS.ocr(self.device.image)
        if text_pos != (0, 0, 0, 0):
            # 如果能找得到 未通关 ，那可以挑战
            layer = confirm_layer(self.O_SE_JADE, text_pos)
            if layer:
                self.C_SE_CLICK_LAYER.roi_front = text_pos
                self.click(self.C_SE_CLICK_LAYER, interval=1)
                return layer
            else:
                return None

        else:
            # 如果不是就向下滑动，继续找或者是判断
            last_text_pos = self.O_SE_NO_PASS_LAST.ocr(self.device.image)
            if last_text_pos != (0, 0, 0, 0):
                # 如果是后面找得到
                layer = confirm_layer(self.O_SE_JADE, last_text_pos)

                # 有个bug 十层的发现不了
                if not layer and last_text_pos[1] > 520:
                    layer = 10

                if layer:
                    self.C_SE_CLICK_LAYER.roi_front = last_text_pos
                    self.click(self.C_SE_CLICK_LAYER, interval=1)
                    return layer
                else:
                    return None

            else:
                # 如果不是就一直滑动
                self.swipe(self.S_SE_DOWN_SEIPE, interval=3)
                time.sleep(2)

    def click_battle(self):
        while 1:
            self.screenshot()
            if not self.appear(self.I_SE_FIRE):
                break
            if self.appear_then_click(self.I_SE_FIRE, interval=1):
                continue

    def check_time(self) -> None:
        """
        周一早上不能打
        @return:
        """
        time_now = datetime.now()
        if time_now.weekday() == 0 and time_now.hour < 8:
            self.set_next_run(task='Secret',
                              finish=True,
                              target=time_now.replace(hour=9, minute=0, second=0, microsecond=0))
            raise TaskEnd('Secret')


if __name__ == '__main__':
    from module.config.config import Config
    from module.device.device import Device

    c = Config('oas1')
    d = Device(c)
    t = ScriptTask(c, d)
    t.screenshot()

    t.run()
