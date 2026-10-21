# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import re
from time import sleep
from datetime import time, datetime, timedelta
from cached_property import cached_property

from module.logger import logger
from module.exception import TaskEnd
from module.base.timer import Timer


from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.matcher import any_of
from tasks.GameUi.page import page_main, page_team, page_shikigami_records
from tasks.Component.GeneralBattle.general_battle import GeneralBattle, ExitMatcher
from tasks.Component.GeneralBattle.config_general_battle import GeneralBattleConfig
from tasks.Component.GeneralRoom.general_room import GeneralRoom
from tasks.Component.GeneralInvite.general_invite import GeneralInvite
from tasks.Component.GeneralBuff.config_buff import BuffClass
from tasks.Component.SwitchSoul.switch_soul import SwitchSoul
from tasks.Restart.server_update import delay_pending_tasks_for_server_update
from tasks.Tako.assets import TakoAssets
from tasks.Tako.config import UserStatus


#石距和愤怒的石距
class ScriptTask(GameUi, GeneralBattle, GeneralRoom, GeneralInvite, SwitchSoul, TakoAssets):

    def run(self):
        def cd_exit(cd: timedelta=False):
            logger.warning(f'石距在CD: {cd}')
            if cd is False:
                self.set_next_run(task='Tako', success=False, finish=True)
                raise TaskEnd('Tako')
            next_run = datetime.now() + cd
            self.set_next_run(task='Tako', success=False, finish=False, target=next_run)
            raise TaskEnd('Tako')

        conf = self.config.tako
        if conf.switch_soul.enable:
            self.goto_page(page_shikigami_records)
            self.run_switch_soul(conf.switch_soul.switch_group_team)

        if conf.switch_soul.enable_switch_by_name:
            self.goto_page(page_shikigami_records)
            self.run_switch_soul_by_name(conf.switch_soul.group_name,
                                         conf.switch_soul.team_name)
        # 加成
        conf_buff = conf.tako_config

        # 进入
        self.goto_page(page_team)
        if 5 <= self.start_time.weekday() <= 6:
            # 周末
            self.check_zones('愤怒的石距', '喷怒的石距', '愤恕的石距', '喷恕的石距')
        else:
            self.check_zones('石距')

        if conf.tako_config.user_status == UserStatus.LEADER:
            cd = self.check_cd()
            if cd:
                cd_exit(cd)
            # 但是这个时候还不一定可以完全判断有没有进入cd
            if not self.create_room():
                self.exit_task()
            # 创建成功，继续
            self.ensure_public()
            self.create_ensure()
            # 创建房间后，先等邀请按钮出现，说明房间界面已就绪
            if not self.wait_until_appear(self.I_ADD_1, wait_time=10):
                logger.warning('Room not ready, exit')
                self.exit_room()
                self.exit_task()
            # 进入到了房间里面
            wait_timer = Timer(60)
            wait_timer.start()
            while 1:
                self.screenshot()
                if not self.is_in_room():
                    continue
                if wait_timer.reached():
                    logger.warning('Wait for too long, exit')
                    self.exit_room()
                    break
                if not self.appear(self.I_ADD_1):
                    # 二次确认，防止界面刷新瞬间误判
                    self.device.sleep(0.5)
                    self.screenshot()
                    if self.appear(self.I_ADD_1):
                        continue
                    # 有人进来了，可以进行挑战
                    logger.info('There is someone in the room and start the challenge')
                    self.click_fire()
                    buff = []
                    if conf_buff.buff_gold_50_click:
                        buff.append(BuffClass.GOLD_50)
                    if conf_buff.buff_gold_100_click:
                        buff.append(BuffClass.GOLD_100)
                    if conf_buff.buff_exp_50_click:
                        buff.append(BuffClass.EXP_50)
                    if conf_buff.buff_exp_100_click:
                        buff.append(BuffClass.EXP_100)
                    if not buff:
                        buff = None
                    self.run_general_battle(config=self.battle_config, buff=buff)
                    break
            self.exit_task()

        if conf.tako_config.user_status == UserStatus.MEMBER:
            cd = self.check_cd()
            if cd:
                cd_exit(cd)
            # 但是这个时候还不一定可以完全判断有没有进入cd
            cd_count = 0
            while 1:
                self.screenshot()
                if self.appear(self.I_TAKO_WAITING):
                    break
                if cd_count >= 6:
                    # 6 x 1.5 = 9秒没有进入说明是在冷却中
                    cd_exit()
                if self.appear_then_click(self.I_GR_AUTO_MATCH, interval=1.5):
                    cd_count += 1
                    continue
            # 匹配个8分钟，要是八分钟还没人拿没啥了
            logger.info('Waiting for match')
            click_timer = Timer(240)
            check_timer = Timer(480)
            click_timer.start()
            check_timer.start()
            self.device.stuck_record_add('LOGIN_CHECK')
            while 1:
                self.screenshot()
                # 如果被秒开进入战斗, 被秒开不支持开启buff
                if self.check_take_over_battle(False, config=self.battle_config):
                    logger.info('tako take over battle')
                    break
                # 如果进入房间
                elif self.is_in_room():
                    self.device.stuck_record_clear()
                    if self.wait_battle(wait_time=time(minute=1)):
                        buff = []
                        if conf_buff.buff_gold_50_click:
                            buff.append(BuffClass.GOLD_50)
                        if conf_buff.buff_gold_100_click:
                            buff.append(BuffClass.GOLD_100)
                        if conf_buff.buff_exp_50_click:
                            buff.append(BuffClass.EXP_50)
                        if conf_buff.buff_exp_100_click:
                            buff.append(BuffClass.EXP_100)
                        if not buff:
                            buff = None
                        self.run_general_battle(config=self.battle_config, buff=buff)
                        # 打完后返回庭院，记得关闭buff
                        if conf_buff.enable:
                            self.goto_page(page_main)
                            self.open_buff()
                            if conf_buff.buff_gold_50_click:
                                self.gold_50(False)
                            if conf_buff.buff_gold_100_click:
                                self.gold_100(False)
                            if conf_buff.buff_exp_50_click:
                                self.exp_50(False)
                            if conf_buff.buff_exp_100_click:
                                self.exp_100(False)
                            self.close_buff()
                        break
                    else:
                        break
                # 如果时间到了
                if click_timer and click_timer.reached():
                    logger.warning('It has waited for 240s , but the battle has not started.')
                    logger.warning('It will be waited for 240s and try again.')
                    self.screenshot()
                    self.click(self.C_TAKO_CLIC_SAFE)
                    click_timer = None
                    self.device.stuck_record_clear()
                    self.device.stuck_record_add('LOGIN_CHECK')
                    continue

                if check_timer.reached():
                    logger.warning('tako match timeout')
                    while 1:
                        self.device.sleep(0.5)
                        self.screenshot()
                        # 先处理各种确认框
                        if self.appear_then_click(self.I_UI_CONFIRM, interval=1):
                            continue
                        if self.appear_then_click(self.I_UI_CONFIRM_SAMLL, interval=1):
                            continue

                        # 匹配界面消失，且没有确认框，才退出
                        if not self.appear(self.I_TAKO_WAITING) and not self.appear(self.I_TAKO_WAITING_1):
                            break
                        if self.appear_then_click(self.I_TAKO_WAITING, interval=1):
                            continue
                    logger.info('tako match timeout, exit')
                    break
                # 如果还在匹配中
                if self.appear(self.I_TAKO_WAITING):
                    continue

            # 退出结束
            self.set_next_run(task='Tako', success=True, finish=False)
            raise TaskEnd('Tako')

    def check_cd(self) -> False or timedelta:
        """
        检查是否在冷却中,
        :return: 如果是冷却中, 返回剩余时间, 否则返回False
        """
        self.screenshot()
        if not self.appear(self.I_TAKO_CHECK):
            return False
        if not self.ocr_appear(self.O_TAKO_BATTLE_AGAIN):
            return False
        # 可以判断是在冷却了
        result = self.O_TAKO_CD.ocr(self.device.image)
        if not isinstance(result, str):
            logger.error(f'OCR error {result}')
            return False
        try:
            result = re.search(r'(\d+)时(\d+)分后可', result)
            time_delta = timedelta(hours=int(result.group(1)), minutes=int(result.group(2)))
        except Exception as e:
            logger.error(f'{e} {result}')
            time_delta = timedelta(hours=4)
        return time_delta

    @cached_property
    def battle_config(self) -> GeneralBattleConfig:
        con = GeneralBattleConfig()
        return con



    def exit_task(self):
        """
        退出任务
        :return:
        """
        conf_buff = self.config.tako.tako_config
        if conf_buff.enable:
            self.goto_page(page_main)
            self.open_buff()
            if conf_buff.buff_gold_50_click:
                self.gold_50(False)
            if conf_buff.buff_gold_100_click:
                self.gold_100(False)
            if conf_buff.buff_exp_50_click:
                self.exp_50(False)
            if conf_buff.buff_exp_100_click:
                self.exp_100(False)
            self.close_buff()
        else:
            self.finish_task_navigation()

        self.set_next_run(task='Tako', success=True, finish=False)
        raise TaskEnd('Tako')

    def _exit_matcher(self) -> ExitMatcher | None:
        return any_of(self.I_CHECK_MAIN, self.I_CHECK_TEAM)


if __name__ == '__main__':
    from module.config.config import Config
    from module.device.device import Device
    c = Config('oas1')
    d = Device(c)
    t = ScriptTask(c, d)
    t.screenshot()

    t.run()
