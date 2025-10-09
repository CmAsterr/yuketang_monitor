import sys
import time
import threading
import datetime
import os
import json
import re
from PyQt6.QtWidgets import (QApplication, QMainWindow, QTabWidget, QWidget, QVBoxLayout, 
                            QHBoxLayout, QPushButton, QLabel, QTextEdit, QMessageBox, 
                            QInputDialog, QFileDialog, QGroupBox, QFormLayout, QLineEdit,
                            QCheckBox, QComboBox, QSpinBox, QTextBrowser, QGridLayout,
                            QDialog, QScrollArea, QProgressDialog)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QFont, QIcon, QTextCursor

# 导入项目现有模块
import utils
import config_manager
import course_manager
import browser_manager
import notification_manager
import ai_manager

class LoginThread(QThread):
    """登录线程，严格遵循命令行登录流程"""
    log_signal = pyqtSignal(str)
    status_signal = pyqtSignal(str)
    login_success = pyqtSignal(object, str)  # 传递driver和course_url
    login_failed = pyqtSignal(str)
    need_user_confirm = pyqtSignal(int)  # 1: 扫码完成确认, 2: 课程页面确认

    def __init__(self, server_url, course_url=None):
        super().__init__()
        self.server_url = server_url
        self.course_url = course_url  # 已保存的课程URL
        self.running = True
        self.driver = None
        self.confirm_step1 = False  # 扫码完成确认
        self.confirm_step2 = False  # 课程页面确认

    def run(self):
        try:
            # 初始化浏览器（与命令行一致）
            self.status_signal.emit("初始化浏览器")
            self.log_signal.emit("【初始化浏览器】")
            self.driver = browser_manager.init_browser()
            if not self.driver:
                self.log_signal.emit("浏览器初始化失败，尝试重新初始化...")
                self.driver = browser_manager.init_browser()  # 重试1次
                if not self.driver:
                    self.login_failed.emit("浏览器初始化失败（共2次尝试）")
                    return

            # 步骤1：访问登录页面（与命令行完全一致）
            login_url = f"{self.server_url}/web"
            self.status_signal.emit("访问登录页面")
            self.log_signal.emit(f"步骤1/3：访问登录页面: {login_url}")
            self.driver.get(login_url)
            time.sleep(4)

            # 等待用户扫码登录（触发UI确认）
            self.need_user_confirm.emit(1)
            while self.running and not self.confirm_step1:
                time.sleep(1)

            if not self.running:
                return

            # 步骤2：等待用户导航到课程页面（触发UI确认）
            self.status_signal.emit("等待导航到课程页面")
            self.need_user_confirm.emit(2)
            while self.running and not self.confirm_step2:
                time.sleep(1)

            if not self.running:
                return

            # 步骤3：获取当前URL
            self.status_signal.emit("获取课程URL")
            self.log_signal.emit("步骤3/3：获取课程页面URL")
            current_url = browser_manager.get_active_tab_url(self.driver)
            
            # 处理URL获取失败情况（与命令行一致）
            course_url = None
            if not current_url:
                self.log_signal.emit("无法自动获取URL，需要手动输入")
                self.status_signal.emit("请手动输入课程URL")
                return
            else:
                # 提取课程ID并生成标准URL（与命令行一致）
                course_id = browser_manager.extract_course_id(current_url)
                if course_id:
                    course_url = f"{self.server_url}/lesson/fullscreen/v3/{course_id}"
                else:
                    course_url = current_url

            # 确认URL有效性（与命令行一致）
            if course_url and self.server_url in course_url and "lesson" in course_url:
                self.log_signal.emit(f"获取到有效课程URL: {course_url}")
                self.login_success.emit(self.driver, course_url)
            else:
                self.log_signal.emit(f"获取的URL无效: {course_url}")
                self.status_signal.emit("请手动输入课程URL")

        except Exception as e:
            self.log_signal.emit(f"登录过程错误: {str(e)}")
            self.login_failed.emit(f"登录失败: {str(e)}")
        finally:
            if not self.login_success and self.driver:
                try:
                    self.driver.quit()
                except:
                    pass

    def confirm_scan(self):
        """确认扫码完成"""
        self.confirm_step1 = True

    def confirm_course_page(self):
        """确认已导航到课程页面"""
        self.confirm_step2 = True

    def set_manual_url(self, url):
        """设置手动输入的URL"""
        if url and self.server_url in url and "lesson" in url:
            self.log_signal.emit(f"用户手动输入课程URL: {url}")
            self.login_success.emit(self.driver, url)
        else:
            self.login_failed.emit("无效的课程URL，需包含服务器地址和'lesson'")

    def stop(self):
        self.running = False
        if self.driver:
            try:
                self.driver.quit()
            except:
                pass
        self.wait()


class MonitorThread(QThread):
    """监控线程，完全对齐命令行监控逻辑"""
    log_signal = pyqtSignal(str)
    status_signal = pyqtSignal(str)
    stats_signal = pyqtSignal(dict)
    new_page_signal = pyqtSignal(str, str)  # 页面类型, 页面编号
    error_occurred = pyqtSignal(str)

    def __init__(self, course_dir, course_url, user_config, driver):
        super().__init__()
        self.course_dir = course_dir
        self.course_url = course_url
        self.user_config = user_config
        self.driver = driver  # 继承登录线程的浏览器实例
        self.running = True
        self.history = {
            "ppt": set(),
            "exercise": set(),
            "blank": set(),
            "subjective": set()
        }
        self.stats = {
            "total_cycles": 0,
            "new_pages_detected": 0,
            "errors_occurred": 0
        }
        self.interval_time = user_config['timing']['normal_interval']
        self.rapid_mode_start = 0
        self.last_succ_detect = time.time()
        self.max_consec_errors = 3
        self.consec_errors = 0
        self.wechat_hook = user_config['wechat']['webhook_url']
        self.page_name_map = {
            "ppt": "PPT",
            "exercise": "选择题",
            "blank": "填空题",
            "subjective": "主观题"
        }
        
        # 初始化课程专属目录（与命令行一致）
        utils.init_directories(course_dir)
        # 加载历史记录
        self._load_history()

    def _load_history(self):
        """加载课程历史记录，与命令行版本一致"""
        course_info_path = os.path.join(self.course_dir, "course_info.json")
        if os.path.exists(course_info_path):
            try:
                with open(course_info_path, "r", encoding="utf-8") as f:
                    course_info = json.load(f)
                    for page_type in self.history:
                        if page_type in course_info.get("history", {}):
                            self.history[page_type] = set(course_info["history"][page_type])
                self.log_signal.emit("已加载课程历史记录")
            except Exception as e:
                self.log_signal.emit(f"加载历史记录失败: {str(e)}")

    def run(self):
        try:
            # 导航到课程页面（与命令行一致）
            self.log_signal.emit(f"导航到课程页面: {self.course_url}")
            self.driver.get(self.course_url)
            time.sleep(3)
            browser_manager.handle_all_alerts(self.driver)

            # 输出监控开始信息（格式与命令行一致）
            self.log_signal.emit("\n" + "="*60)
            self.log_signal.emit("          开始进入监控循环（按停止按钮停止）")
            self.log_signal.emit("="*60)
            self.log_signal.emit(f"监控配置: 常规间隔={self.user_config['timing']['normal_interval']}s | 快速间隔={self.user_config['timing']['rapid_interval']}s")
            self.log_signal.emit(f"          快速阈值={self.user_config['timing']['threshold']}s | 刷新设置={'启用' if self.user_config['refresh'] else '禁用'}")
            self.log_signal.emit(f"          AI分析: {'启用' if self.user_config['ai']['enable'] else '禁用'} | OCR: {'已配置' if (self.user_config['ocr']['apikey'] and self.user_config['ocr']['secretkey']) else '未配置'}")
            self.log_signal.emit(f"          课程目录: {self.course_dir}")
            self.log_signal.emit("="*60 + "\n")
            
            self.status_signal.emit("监控中")

            while self.running:
                # 更新统计信息
                self.stats["total_cycles"] += 1
                current_cycle = self.stats["total_cycles"]
                cycle_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                # 日志记录周期信息（完全对齐命令行格式）
                self.log_signal.emit(f"\n[检测周期 {current_cycle}] 开始时间: {cycle_time}")
                self.log_signal.emit(f"当前状态: 间隔={self.interval_time}s | 快速模式={'已启用' if self.rapid_mode_start else '未启用'}")
                self.log_signal.emit(f"性能统计: 新页面={self.stats['new_pages_detected']} | 错误数={self.stats['errors_occurred']} | 连续错误={self.consec_errors}")
                
                # 发送统计信息到UI
                self.stats_signal.emit(self.stats.copy())
                
                # 定期保存课程数据（每10个周期，与命令行一致）
                if current_cycle % 10 == 0:
                    course_manager.save_course_data(
                        self.course_dir, 
                        self.course_url, 
                        self.user_config['server']['name'], 
                        self.history, 
                        self.stats
                    )
                    self.log_signal.emit("课程数据已保存")

                # 3分钟未成功检测，强制重连浏览器（与命令行一致）
                if time.time() - self.last_succ_detect > 3 * 60:
                    self.log_signal.emit("【警告】已超过3分钟未成功检测，尝试重连浏览器...")
                    if self.driver:
                        self.driver.quit()
                    self.driver = browser_manager.reconnect_browser(self.course_url)
                    self.consec_errors = 0
                
                # 浏览器未连接，尝试重连
                if not self.driver:
                    self.driver = browser_manager.reconnect_browser(self.course_url)
                    if not self.driver:
                        self.log_signal.emit(f"无法重新连接浏览器，{self.interval_time}秒后重试...")
                        time.sleep(self.interval_time)
                        continue

                try:
                    # 按配置刷新页面（与命令行一致）
                    if self.user_config['refresh']:
                        self.log_signal.emit("执行页面刷新操作...")
                        browser_manager.handle_all_alerts(self.driver)  # 处理刷新前弹窗
                        self.driver.get(self.course_url)
                        time.sleep(3)  # 等待页面加载
                        browser_manager.handle_all_alerts(self.driver)  # 处理刷新后弹窗
                        self.log_signal.emit("页面刷新完成")
                    
                    # 获取当前页面URL
                    current_page_url = browser_manager.get_active_tab_url(self.driver)
                    if not current_page_url:
                        self.log_signal.emit("无法获取当前页面URL")
                        self.consec_errors += 1
                        self.stats["errors_occurred"] += 1
                        
                        # 连续错误达到阈值，重连浏览器（与命令行一致）
                        if self.consec_errors >= self.max_consec_errors:
                            self.log_signal.emit(f"连续错误次数达到{self.max_consec_errors}次，触发浏览器重连...")
                            self.driver = browser_manager.reconnect_browser(self.course_url)
                            self.consec_errors = 0
                        
                        time.sleep(self.interval_time)
                        continue
                    
                    self.log_signal.emit(f"当前页面URL: {current_page_url}")
                    
                    # 识别页面类型和编号（与命令行正则一致）
                    page_type, page_number = None, None
                    page_patterns = {
                        "ppt": r"/ppt/(\d+)",
                        "exercise": r"/exercise/(\d+)",
                        "blank": r"/blank/(\d+)",
                        "subjective": r"/subjective/(\d+)"
                    }
                    for ptype, pattern in page_patterns.items():
                        match = re.search(pattern, current_page_url)
                        if match:
                            page_type = ptype
                            page_number = match.group(1)
                            break
                    
                    # 未识别到监控页面类型（如课程目录页）
                    if not (page_type and page_number):
                        self.log_signal.emit("未识别到已知页面类型（可能在课程目录页）")
                        self.consec_errors = 0
                        self.last_succ_detect = time.time()
                        time.sleep(self.interval_time)
                        continue
                    
                    self.log_signal.emit(f"识别到页面类型: {page_type}，编号: {page_number}")
                    
                    # 判断是否为新页面+是否需要监控（下载/通知）
                    is_new_page = page_number not in self.history[page_type]
                    is_monitored_page = (self.user_config['page_settings'][page_type]['notify'] or 
                                       self.user_config['page_settings'][page_type]['download'])
                    
                    if is_monitored_page and is_new_page:
                        # 1. 进入快速模式（与命令行一致）
                        self.rapid_mode_start = time.time()
                        self.interval_time = self.user_config['timing']['rapid_interval']
                        self.log_signal.emit(f"检测到新{page_type}页面，进入快速模式（间隔{self.interval_time}秒）")
                        
                        # 2. 更新历史记录与统计
                        self.history[page_type].add(page_number)
                        self.stats["new_pages_detected"] += 1
                        
                        # 3. 发送新页面信号到UI
                        self.new_page_signal.emit(page_type, page_number)
                        
                        # 4. 定位页面元素并下载图片（按配置）
                        image_path = None
                        try:
                            # 定位页面核心元素
                            base_element = browser_manager.locate_page_element(
                                self.driver, page_type, self.user_config['xpaths'])
                            if base_element:
                                # 查找图片元素
                                img_elements = base_element.find_elements("tag name", "img")
                                if img_elements:
                                    img_src = img_elements[0].get_attribute("src") or img_elements[0].get_attribute("data-src")
                                    if img_src:
                                        self.log_signal.emit(f"获取到{page_type}页面图片资源: {img_src[:100]}...")
                                        # 按配置下载图片
                                        if self.user_config['page_settings'][page_type]['download']:
                                            image_path = utils.download_image(
                                                img_src, self.course_dir, page_type, page_number)
                        
                        except Exception as e:
                            self.log_signal.emit(f"{page_type}页面元素处理异常: {str(e)}")
                            self.stats["errors_occurred"] += 1
                        
                        # 5. 发送通知（系统通知+微信通知）
                        if self.user_config['page_settings'][page_type]['notify']:
                            # 发送系统通知
                            
                            notification_manager.send_system_notification(
                                title=f"新{page_type}页面",
                                message=f"编号: {page_number}\n时间: {cycle_time}\n课程URL: {self.course_url}"
                            )
                            
                            # 发送企业微信通知（按配置）
                            if self.wechat_hook:
                                notification_manager.send_wechat_notification(
                                    webhook_url=self.wechat_hook,
                                    title=f"新{self.page_name_map[page_type]}页面提醒",
                                    content=f"页面编号：{page_number}\n课程URL：{self.course_url}",
                                    image_path=image_path
                                )
                        # 6. 用 ai 解答
                        if self.user_config['ai']['enable'] and image_path:
                            def ai_process_and_notify():
                                ai_answer = ai_manager.get_ai_answer(
                                    ocr_config=self.user_config['ocr'],
                                    ai_config=self.user_config['ai'],
                                    image_path=image_path
                                )
                                
                                if self.wechat_hook and ai_answer:
                                    notification_manager.send_ai_notification(
                                        webhook_url=self.wechat_hook,
                                        title=f"新{self.page_name_map[page_type]}页面分析",
                                        content=ai_answer,
                                        )
                                self.log_signal.emit(f"AI分析完成: {ai_answer[:100]}..." if ai_answer else "AI分析未获取到结果")
                            
                            ai_thread = threading.Thread(target=ai_process_and_notify)
                            ai_thread.daemon = True  # 设为守护线程，主程序退出时自动关闭
                            ai_thread.start()  # 启动线程（异步执行，不阻塞主循环）
                    
                    elif is_monitored_page and not is_new_page:
                        # 快速模式超时判断（恢复常规间隔）
                        if self.rapid_mode_start and (time.time() - self.rapid_mode_start) > self.user_config['timing']['threshold']:
                            self.interval_time = self.user_config['timing']['normal_interval']
                            self.log_signal.emit(f"快速模式已持续{(time.time() - self.rapid_mode_start):.1f}秒（超过阈值{self.user_config['timing']['threshold']}秒），恢复常规间隔{self.interval_time}秒")
                            self.rapid_mode_start = 0
                        elif self.rapid_mode_start:
                            self.log_signal.emit(f"快速模式持续中（{(time.time() - self.rapid_mode_start):.1f}秒），保持间隔{self.interval_time}秒")
                    
                    else:
                        self.log_signal.emit(f"当前页面类型({page_type})未配置监控，保持间隔{self.interval_time}秒")
                    
                    # 重置错误计数与检测时间
                    self.consec_errors = 0
                    self.last_succ_detect = time.time()
                    browser_manager.handle_all_alerts(self.driver)
                    
                    # 等待下一个检测周期
                    self.log_signal.emit(f"检测周期{current_cycle}完成，{self.interval_time}秒后进行下一次检测...")
                    time.sleep(self.interval_time)
                
                except Exception as e:
                    self.log_signal.emit(f"【错误】检测周期中发生异常: {str(e)}")
                    self.error_occurred.emit(str(e))
                    self.consec_errors += 1
                    self.stats["errors_occurred"] += 1
                    if self.consec_errors >= self.max_consec_errors:
                        self.log_signal.emit(f"连续错误次数达到{self.max_consec_errors}次，触发重新连接...")
                        self.driver = browser_manager.reconnect_browser(self.course_url)
                        self.consec_errors = 0
                    time.sleep(self.interval_time)
        
        except Exception as e:
            self.log_signal.emit(f"【严重错误】监控主循环异常终止: {str(e)}")
            self.status_signal.emit(f"监控异常: {str(e)}")
            self.error_occurred.emit(str(e))
        finally:
            # 退出前保存课程数据（与命令行一致）
            course_manager.save_course_data(
                self.course_dir, 
                self.course_url, 
                self.user_config['server']['name'], 
                self.history, 
                self.stats
            )
            
            # 输出统计信息（格式与命令行一致）
            self.log_signal.emit("\n" + "="*60)
            self.log_signal.emit("          监控程序结束 - 统计信息")
            self.log_signal.emit("="*60)
            self.log_signal.emit(f"总检测周期: {self.stats['total_cycles']}次")
            self.log_signal.emit(f"检测到新页面: {self.stats['new_pages_detected']}个")
            self.log_signal.emit(f"发生错误次数: {self.stats['errors_occurred']}次")
            self.log_signal.emit(f"PPT页面历史: {len(self.history['ppt'])}个")
            self.log_signal.emit(f"选择题页面历史: {len(self.history['exercise'])}个")
            self.log_signal.emit(f"填空题页面历史: {len(self.history['blank'])}个")
            self.log_signal.emit(f"主观题页面历史: {len(self.history['subjective'])}个")
            self.log_signal.emit(f"课程数据已保存至: {self.course_dir}")
            self.log_signal.emit("="*60)
            
            # 关闭浏览器
            if self.driver:
                try:
                    browser_manager.handle_all_alerts(self.driver)
                    self.driver.quit()
                    self.log_signal.emit("浏览器已成功关闭")
                except Exception as e:
                    self.log_signal.emit(f"关闭浏览器时发生错误: {str(e)}")
            
            self.status_signal.emit("监控已停止")

    def stop(self):
        self.running = False
        self.wait()


class LoginDialog(QDialog):
    """登录对话框，实现命令行中的交互步骤"""
    confirm_scan = pyqtSignal()
    confirm_course = pyqtSignal()
    manual_url = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("用户登录")
        self.setModal(True)
        self.resize(500, 300)
        
        layout = QVBoxLayout()
        
        # 状态显示
        self.status_label = QLabel("准备登录...")
        self.status_label.setWordWrap(True)
        
        # 日志显示
        self.log_display = QTextEdit()
        self.log_display.setReadOnly(True)
        self.log_display.setMinimumHeight(150)
        
        # 按钮布局
        self.btn_layout = QHBoxLayout()
        
        # 扫码完成按钮
        self.scan_btn = QPushButton("1. 我已完成扫码登录")
        self.scan_btn.setEnabled(False)
        self.scan_btn.clicked.connect(self.on_scan_confirm)
        
        # 课程页面按钮
        self.course_btn = QPushButton("2. 我已导航到课程页面")
        self.course_btn.setEnabled(False)
        self.course_btn.clicked.connect(self.on_course_confirm)
        
        # 手动输入URL按钮和输入框
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("请输入课程URL...")
        self.url_btn = QPushButton("确认URL")
        self.url_btn.clicked.connect(self.on_url_confirm)
        self.url_input.setEnabled(False)
        self.url_btn.setEnabled(False)
        
        # 取消按钮
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.clicked.connect(self.reject)
        
        # 添加按钮到布局
        self.btn_layout.addWidget(self.scan_btn)
        self.btn_layout.addWidget(self.course_btn)
        self.btn_layout.addWidget(self.url_input)
        self.btn_layout.addWidget(self.url_btn)
        self.btn_layout.addWidget(self.cancel_btn)
        
        layout.addWidget(self.status_label)
        layout.addWidget(self.log_display)
        layout.addLayout(self.btn_layout)
        
        self.setLayout(layout)
    
    def update_status(self, text):
        self.status_label.setText(text)
    
    def append_log(self, text):
        self.log_display.append(text)
        self.log_display.moveCursor(QTextCursor.MoveOperation.End)
    
    def enable_scan_confirm(self):
        self.scan_btn.setEnabled(True)
        self.status_label.setText("请在浏览器中完成扫码登录，完成后点击'1. 我已完成扫码登录'按钮")
    
    def enable_course_confirm(self):
        self.scan_btn.setEnabled(False)
        self.course_btn.setEnabled(True)
        self.status_label.setText("请在浏览器中导航到需要监控的课程页面，完成后点击'2. 我已导航到课程页面'按钮")
    
    def enable_url_input(self):
        self.course_btn.setEnabled(False)
        self.url_input.setEnabled(True)
        self.url_btn.setEnabled(True)
        self.status_label.setText("请手动输入课程URL（需包含'lesson'）:")
    
    def on_scan_confirm(self):
        self.confirm_scan.emit()
    
    def on_course_confirm(self):
        self.confirm_course.emit()
    
    def on_url_confirm(self):
        url = self.url_input.text().strip()
        if url:
            self.manual_url.emit(url)


# -------------------------- 新增：环境检测对话框 --------------------------
class EnvCheckDialog(QDialog):
    """环境检测结果对话框，包含文本框显示检测信息"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("环境检测结果")
        self.setModal(True)
        self.resize(700, 500)  # 适中尺寸，避免屏幕放不下
        
        # 布局设计
        main_layout = QVBoxLayout(self)
        
        # 标题
        title_label = QLabel("驱动与版本检测报告")
        title_font = QFont()
        title_font.setPointSize(12)
        title_font.setBold(True)
        title_label.setFont(title_font)
        main_layout.addWidget(title_label)
        
        # 检测结果文本框（带滚动条）
        self.result_text = QTextEdit()
        self.result_text.setReadOnly(True)
        self.result_text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)  # 日志类内容不自动换行
        main_layout.addWidget(self.result_text, stretch=1)  # 占满剩余空间
        
        # 关闭按钮
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        main_layout.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignRight)
    
    def set_result(self, result_text):
        """设置检测结果文本"""
        self.result_text.setText(result_text)
        # 滚动到开头
        self.result_text.moveCursor(QTextCursor.MoveOperation.Start)


# -------------------------- 修改：配置界面集成环境检测按钮 --------------------------
class ConfigWidget(QWidget):
    """配置界面（新增环境检测按钮）"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent = parent
        self.init_ui()
    
    def init_ui(self):
        # 创建滚动区域
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        
        # 创建内容部件
        self.content_widget = QWidget()
        self.setup_content()
        
        self.scroll_area.setWidget(self.content_widget)
        
        # 主布局
        main_layout = QVBoxLayout()
        main_layout.addWidget(self.scroll_area)
        self.setLayout(main_layout)
    
    def setup_content(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(15)
        
        # -------------------------- 新增：环境检测按钮 --------------------------
        # 单独的环境检测区域
        env_check_group = QGroupBox("环境检测")
        env_check_layout = QHBoxLayout()
        
        self.env_check_btn = QPushButton("驱动与版本检测")
        self.env_check_btn.clicked.connect(self.run_environment_check)  # 绑定检测逻辑
        env_check_layout.addWidget(self.env_check_btn)
        
        # 检测状态提示
        self.env_check_status = QLabel("点击按钮进行驱动和版本兼容性检测")
        env_check_layout.addWidget(self.env_check_status)
        
        env_check_group.setLayout(env_check_layout)
        layout.addWidget(env_check_group)
        # ----------------------------------------------------------------------
        
        # 加载当前配置
        self.config = config_manager.load_config()
        
        # 服务器配置
        server_group = QGroupBox("服务器配置")
        server_layout = QFormLayout()
        
        self.server_combo = QComboBox()
        servers = [
            {"id": 1, "name": "雨课堂", "base_url": "https://www.yuketang.cn"},
            {"id": 2, "name": "荷塘雨课堂", "base_url": "https://pro.yuketang.cn"},
            {"id": 3, "name": "长江雨课堂", "base_url": "https://changjiang.yuketang.cn"},
            {"id": 4, "name": "黄河雨课堂", "base_url": "https://huanghe.yuketang.cn"}
        ]
        
        for server in servers:
            self.server_combo.addItem(f"{server['name']} ({server['base_url']})", server)
        
        # 选中当前服务器
        current_server_name = self.config['server']['name']
        for i, server in enumerate(servers):
            if server['name'] == current_server_name:
                self.server_combo.setCurrentIndex(i)
                break
        
        server_layout.addRow("选择服务器:", self.server_combo)
        server_group.setLayout(server_layout)
        layout.addWidget(server_group)
        
        # 时间配置
        timing_group = QGroupBox("时间配置")
        timing_layout = QFormLayout()
        
        self.normal_interval = QSpinBox()
        self.normal_interval.setRange(1, 300)
        self.normal_interval.setValue(self.config['timing']['normal_interval'])
        
        self.threshold = QSpinBox()
        self.threshold.setRange(10, 300)
        self.threshold.setValue(self.config['timing']['threshold'])
        
        self.rapid_interval = QSpinBox()
        self.rapid_interval.setRange(1, 60)
        self.rapid_interval.setValue(self.config['timing']['rapid_interval'])
        
        timing_layout.addRow("常规检测间隔(秒):", self.normal_interval)
        timing_layout.addRow("快速模式阈值(秒):", self.threshold)
        timing_layout.addRow("快速检测间隔(秒):", self.rapid_interval)
        timing_group.setLayout(timing_layout)
        layout.addWidget(timing_group)
        
        # 页面设置
        page_settings_group = QGroupBox("页面监控设置")
        page_layout = QGridLayout()
        
        # 页面类型描述
        page_types = {
            "ppt": "PPT页面",
            "exercise": "选择题页面",
            "blank": "填空题页面",
            "subjective": "主观题页面"
        }
        
        # 标题行
        page_layout.addWidget(QLabel("页面类型"), 0, 0)
        page_layout.addWidget(QLabel("自动下载"), 0, 1)
        page_layout.addWidget(QLabel("发送通知"), 0, 2)
        
        # 页面类型设置行
        self.page_download_checks = {}
        self.page_notify_checks = {}
        
        for row, (page_key, page_name) in enumerate(page_types.items(), 1):
            page_layout.addWidget(QLabel(page_name), row, 0)
            
            # 下载复选框
            download_check = QCheckBox()
            download_check.setChecked(self.config['page_settings'][page_key]['download'])
            self.page_download_checks[page_key] = download_check
            page_layout.addWidget(download_check, row, 1)
            
            # 通知复选框
            notify_check = QCheckBox()
            notify_check.setChecked(self.config['page_settings'][page_key]['notify'])
            self.page_notify_checks[page_key] = notify_check
            page_layout.addWidget(notify_check, row, 2)
        
        page_settings_group.setLayout(page_layout)
        layout.addWidget(page_settings_group)
        
        # 刷新配置
        refresh_group = QGroupBox("刷新配置")
        refresh_layout = QHBoxLayout()
        
        self.refresh_check = QCheckBox("每次检测前刷新页面")
        self.refresh_check.setChecked(self.config['refresh'])
        
        refresh_layout.addWidget(self.refresh_check)
        refresh_group.setLayout(refresh_layout)
        layout.addWidget(refresh_group)
        
        # 微信配置
        wechat_group = QGroupBox("微信消息配置")
        wechat_layout = QFormLayout()
        
        self.wechat_hook = QLineEdit()
        self.wechat_hook.setText(self.config['wechat']['webhook_url'])
        
        wechat_layout.addRow("企业微信机器人WebHook:", self.wechat_hook)
        wechat_group.setLayout(wechat_layout)
        layout.addWidget(wechat_group)
        
        # OCR配置
        ocr_group = QGroupBox("百度OCR配置")
        ocr_layout = QFormLayout()
        
        self.ocr_apikey = QLineEdit()
        self.ocr_apikey.setText(self.config['ocr']['apikey'])
        
        self.ocr_secretkey = QLineEdit()
        self.ocr_secretkey.setText(self.config['ocr']['secretkey'])
        
        ocr_layout.addRow("OCR API Key:", self.ocr_apikey)
        ocr_layout.addRow("OCR Secret Key:", self.ocr_secretkey)
        ocr_group.setLayout(ocr_layout)
        layout.addWidget(ocr_group)
        
        # AI配置
        ai_group = QGroupBox("AI分析配置")
        ai_layout = QFormLayout()
        
        self.ai_enable = QCheckBox("启用AI分析功能")
        self.ai_enable.setChecked(self.config['ai']['enable'])
        
        self.ai_apikey = QLineEdit()
        self.ai_apikey.setText(self.config['ai']['apikey'])
        
        self.ai_baseurl = QLineEdit()
        self.ai_baseurl.setText(self.config['ai']['base_url'])
        
        self.ai_model = QLineEdit()
        self.ai_model.setText(self.config['ai']['model'])
        
        ai_layout.addRow(self.ai_enable)
        ai_layout.addRow("AI API Key:", self.ai_apikey)
        ai_layout.addRow("AI Base URL:", self.ai_baseurl)
        ai_layout.addRow("AI Model:", self.ai_model)
        ai_group.setLayout(ai_layout)
        layout.addWidget(ai_group)
        
        # XPath配置
        xpath_group = QGroupBox("页面元素XPath配置")
        xpath_layout = QFormLayout()
        
        self.ppt_xpath = QLineEdit()
        self.ppt_xpath.setText(self.config['xpaths']['ppt'])
        
        self.exercise_xpath = QLineEdit()
        self.exercise_xpath.setText(self.config['xpaths']['exercise'])
        
        self.blank_xpath = QLineEdit()
        self.blank_xpath.setText(self.config['xpaths']['blank'])
        
        self.subjective_xpath = QLineEdit()
        self.subjective_xpath.setText(self.config['xpaths']['subjective'])
        
        xpath_layout.addRow("PPT页面XPath:", self.ppt_xpath)
        xpath_layout.addRow("选择题页面XPath:", self.exercise_xpath)
        xpath_layout.addRow("填空题页面XPath:", self.blank_xpath)
        xpath_layout.addRow("主观题页面XPath:", self.subjective_xpath)
        xpath_group.setLayout(xpath_layout)
        layout.addWidget(xpath_group)
        
        # 按钮
        btn_layout = QHBoxLayout()
        self.save_btn = QPushButton("保存配置")
        self.save_btn.clicked.connect(self.save_config)
        
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.clicked.connect(self.parent.switch_to_main_tab)
        
        btn_layout.addWidget(self.save_btn)
        btn_layout.addWidget(self.cancel_btn)
        layout.addLayout(btn_layout)
        
        self.content_widget.setLayout(layout)
    
    def run_environment_check(self):
        """执行驱动与版本检测逻辑"""
        self.env_check_status.setText("检测中...")
        self.env_check_btn.setEnabled(False)  # 防止重复点击
        
        # 检测结果收集
        check_result = []
        check_result.append("="*60)
        check_result.append("          雨课堂监控工具 - 环境检测报告")
        check_result.append("="*60 + "\n")
        
        # 1. Python版本检查
        check_result.append("1. Python版本检查:")
        if sys.version_info < (3, 7):
            check_result.append(f"   [错误] 版本过低（需3.7及以上），当前版本: {sys.version.split()[0]}")
            check_result.append("   [建议] 请升级Python版本后重新运行")
        else:
            check_result.append(f"   [通过] 当前版本: {sys.version.split()[0]}（符合要求）")
        check_result.append("")
        
        # 2. 依赖库检查
        check_result.append("2. 必要依赖库检查:")
        required_libs = ["selenium", "requests", "plyer"]
        missing_libs = []
        for lib in required_libs:
            try:
                __import__(lib)
                check_result.append(f"   [通过] {lib}（已安装）")
            except ImportError:
                missing_libs.append(lib)
                check_result.append(f"   [缺失] {lib}（未安装）")
        
        if missing_libs:
            check_result.append(f"   [建议] 执行命令安装缺失库: pip install {' '.join(missing_libs)}")
        check_result.append("")
        
        # 3. Edge驱动检查
        check_result.append("3. Edge驱动检查:")

        if getattr(sys, 'frozen', False):
            # 打包后：获取.exe所在目录
            base_dir = os.path.dirname(sys.executable)
        else:
            # 未打包：使用原逻辑（脚本所在目录）
            base_dir = os.path.dirname(__file__)
        driver_path = os.path.join(base_dir, "msedgedriver.exe")

        check_result.append(driver_path)
        if not os.path.exists(driver_path):
            check_result.append(f"   [警告] 未在当前目录找到驱动: {os.path.basename(driver_path)}")
            check_result.append("   [建议] 下载地址: https://developer.microsoft.com/zh-cn/microsoft-edge/tools/webdriver/")
        else:
            check_result.append(f"   [通过] 驱动文件存在: {driver_path}")
        check_result.append("")
        
        # 4. 浏览器与驱动兼容性检查
        check_result.append("4. 浏览器与驱动兼容性检查:")
        try:
            compatibility = browser_manager.check_edge_compatibility()
            if compatibility:
                check_result.append("   [通过] Edge浏览器与驱动版本兼容")
            else:
                check_result.append("   [错误] 浏览器与驱动版本不兼容")
                check_result.append("   [建议] 下载与浏览器主版本一致的驱动")
        except Exception as e:
            check_result.append(f"   [异常] 兼容性检测失败: {str(e)}")
            check_result.append("   [建议] 手动确认浏览器和驱动版本是否一致")
        check_result.append("")
        
        # 检测总结
        check_result.append("="*60)
        check_result.append("检测总结:")
        if "错误" in "\n".join(check_result):
            check_result.append("   [状态] 存在错误，需修复后运行")
        elif "警告" in "\n".join(check_result):
            check_result.append("   [状态] 存在警告，可继续运行但可能存在风险")
        else:
            check_result.append("   [状态] 所有检测项通过，可正常运行")
        check_result.append("="*60)
        
        # 显示检测结果
        dialog = EnvCheckDialog(self)
        dialog.set_result("\n".join(check_result))
        dialog.exec()
        
        # 恢复按钮状态
        self.env_check_status.setText("点击按钮进行驱动和版本兼容性检测")
        self.env_check_btn.setEnabled(True)
    
    def save_config(self):
        # 更新配置
        selected_server = self.server_combo.currentData()
        self.config['server'] = {
            "name": selected_server['name'],
            "base_url": selected_server['base_url']
        }
        
        self.config['timing'] = {
            "normal_interval": self.normal_interval.value(),
            "threshold": self.threshold.value(),
            "rapid_interval": self.rapid_interval.value()
        }
        
        # 保存页面设置
        for page_key in self.page_download_checks:
            self.config['page_settings'][page_key]['download'] = self.page_download_checks[page_key].isChecked()
            self.config['page_settings'][page_key]['notify'] = self.page_notify_checks[page_key].isChecked()
        
        self.config['refresh'] = self.refresh_check.isChecked()
        self.config['wechat']['webhook_url'] = self.wechat_hook.text()
        
        # 保存OCR配置
        self.config['ocr']['apikey'] = self.ocr_apikey.text()
        self.config['ocr']['secretkey'] = self.ocr_secretkey.text()
        
        # 保存AI配置
        self.config['ai']['enable'] = self.ai_enable.isChecked()
        self.config['ai']['apikey'] = self.ai_apikey.text()
        self.config['ai']['base_url'] = self.ai_baseurl.text()
        self.config['ai']['model'] = self.ai_model.text()
        
        # 保存XPath配置
        self.config['xpaths'] = {
            "ppt": self.ppt_xpath.text(),
            "exercise": self.exercise_xpath.text(),
            "blank": self.blank_xpath.text(),
            "subjective": self.subjective_xpath.text()
        }
        
        # 保存配置
        config_manager.save_config(self.config)
        QMessageBox.information(self, "成功", "配置已保存")
        
        # 通知主窗口配置已更新
        if self.parent:
            self.parent.config_updated()


class CourseManagementWidget(QWidget):
    """课程管理界面"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent = parent
        self.init_ui()
    
    def init_ui(self):
        # 创建滚动区域
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        
        # 创建内容部件
        self.content_widget = QWidget()
        self.setup_content()
        
        self.scroll_area.setWidget(self.content_widget)
        
        # 主布局
        main_layout = QVBoxLayout()
        main_layout.addWidget(self.scroll_area)
        self.setLayout(main_layout)
    
    def setup_content(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(10, 10, 10, 10)
        
        # 课程列表
        self.course_list = QTextBrowser()
        self.course_list.setReadOnly(True)
        
        # 按钮
        btn_layout = QHBoxLayout()
        
        self.new_course_btn = QPushButton("新建课程")
        self.new_course_btn.clicked.connect(self.new_course)
        
        self.open_course_btn = QPushButton("打开课程")
        self.open_course_btn.clicked.connect(self.open_course)

        # 添加删除课程按钮
        self.delete_course_btn = QPushButton("删除课程")
        self.delete_course_btn.clicked.connect(self.delete_course)
        
        self.back_btn = QPushButton("返回主菜单")
        self.back_btn.clicked.connect(self.parent.switch_to_main_tab)
        
        btn_layout.addWidget(self.new_course_btn)
        btn_layout.addWidget(self.open_course_btn)
        btn_layout.addWidget(self.delete_course_btn)  # 添加到布局
        btn_layout.addWidget(self.back_btn)
        
        layout.addWidget(QLabel("已保存的课程:"))
        layout.addWidget(self.course_list)
        layout.addLayout(btn_layout)
        
        self.content_widget.setLayout(layout)
        self.load_courses()
    
    def load_courses(self):
        saved_courses = course_manager.list_saved_courses()
        course_names = sorted(saved_courses.keys())
        
        if course_names:
            self.course_list.clear()
            for i, course_name in enumerate(course_names, 1):
                safe_dir = saved_courses[course_name]
                info_path = os.path.join(course_manager.get_courses_directory(), safe_dir, "course_info.json")
                last_opened = "未知"
                
                if os.path.exists(info_path):
                    try:
                        with open(info_path, "r", encoding="utf-8") as f:
                            course_info = json.load(f)
                            last_opened = course_info.get("last_opened", "未知")
                    except:
                        pass
                
                self.course_list.append(f"{i}. {course_name} (最后打开: {last_opened[:16]})")
        else:
            self.course_list.setText("暂无已保存的课程")
    
    def new_course(self):
        course_name, ok = QInputDialog.getText(self, "新建课程", "请输入新课程名称:")
        if ok and course_name:
            course_dir = course_manager.create_new_course(course_name)
            if course_dir and os.path.exists(course_dir):
                QMessageBox.information(self, "成功", f"新课程 '{course_name}' 创建成功")
                self.load_courses()
                # 通知主窗口打开课程
                self.parent.open_course_monitor(course_dir, "new")
            else:
                QMessageBox.warning(self, "失败", "创建课程失败")
    
    def open_course(self):
        saved_courses = course_manager.list_saved_courses()
        course_names = sorted(saved_courses.keys())
        
        if not course_names:
            QMessageBox.information(self, "提示", "没有已保存的课程")
            return
        
        course_name, ok = QInputDialog.getItem(self, "打开课程", "选择要打开的课程:", course_names, 0, False)
        if ok and course_name:
            safe_dir = saved_courses[course_name]
            result = course_manager.load_course_data(safe_dir)
            if result and len(result) >= 2:
                course_dir, course_url = result[0], result[1]
                if course_dir and os.path.exists(course_dir):
                    self.parent.open_course_monitor(course_dir, "open", course_url)
                else:
                    QMessageBox.warning(self, "错误", "课程数据加载失败")
            else:
                QMessageBox.warning(self, "错误", "课程数据格式不正确")

    # 添加删除课程方法
    def delete_course(self):
        saved_courses = course_manager.list_saved_courses()
        course_names = sorted(saved_courses.keys())
        
        if not course_names:
            QMessageBox.information(self, "提示", "没有已保存的课程")
            return
        
        # 让用户选择要删除的课程
        course_name, ok = QInputDialog.getItem(
            self, 
            "删除课程", 
            "选择要删除的课程:", 
            course_names, 
            0, 
            False
        )
        
        if ok and course_name:
            # 二次确认
            confirm = QMessageBox.question(
                self, 
                "确认删除", 
                f"确定要删除课程 '{course_name}' 吗？此操作不可恢复！",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            
            if confirm == QMessageBox.StandardButton.Yes:
                safe_dir = saved_courses[course_name]
                if course_manager.delete_course(safe_dir):
                    QMessageBox.information(self, "成功", f"课程 '{course_name}' 已成功删除")
                    # 重新加载课程列表
                    self.load_courses()
                    # 如果删除的是当前正在监控的课程，需要更新状态
                    if self.parent.current_course_dir and safe_dir in self.parent.current_course_dir:
                        self.parent.current_course_dir = None
                        self.parent.current_course_url = None
                        self.parent.current_course_label.setText("当前课程: 无")
                        self.parent.course_status.setText("当前课程: 未选择")
                else:
                    QMessageBox.warning(self, "失败", "删除课程失败")


class MainWindow(QMainWindow):
    """主窗口"""
    def __init__(self):
        super().__init__()
        self.init_ui()
        self.monitor_thread = None
        self.login_thread = None
        self.user_config = config_manager.load_config()
        self.current_course_dir = None
        self.current_course_url = None
        self.driver = None  # 浏览器实例，在登录后保存
    
    def init_ui(self):
        self.setWindowTitle("雨课堂页面监控工具")
        self.setGeometry(100, 100, 750, 650)  # 适中尺寸，适配多数屏幕
        
        # 创建中心部件和标签页
        self.central_widget = QTabWidget()
        self.setCentralWidget(self.central_widget)
        
        # 主页面
        self.main_tab = QWidget()
        self.init_main_tab()
        
        # 课程管理页面
        self.course_tab = CourseManagementWidget(self)
        
        # 配置页面（集成环境检测）
        self.config_tab = ConfigWidget(self)
        
        # 监控页面
        self.monitor_tab = QWidget()
        self.init_monitor_tab()
        
        # 添加标签页
        self.central_widget.addTab(self.main_tab, "主菜单")
        self.central_widget.addTab(self.course_tab, "课程管理")
        self.central_widget.addTab(self.config_tab, "配置工具")
        self.central_widget.addTab(self.monitor_tab, "监控中心")
        
        # 状态栏
        self.statusBar().showMessage("就绪")
    
    def init_main_tab(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(20)
        
        # 标题
        title_label = QLabel("雨课堂页面更新检测工具[by CC]")
        title_font = QFont()
        title_font.setPointSize(14)
        title_font.setBold(True)
        title_label.setFont(title_font)
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        # 按钮
        btn_layout = QVBoxLayout()
        btn_layout.setSpacing(15)
        
        self.course_management_btn = QPushButton("课程管理（新建/打开/删除课程）")
        self.course_management_btn.setMinimumHeight(50)
        self.course_management_btn.clicked.connect(lambda: self.central_widget.setCurrentIndex(1))
        
        self.config_btn = QPushButton("配置工具选项")
        self.config_btn.setMinimumHeight(50)
        self.config_btn.clicked.connect(lambda: self.central_widget.setCurrentIndex(2))
        
        self.exit_btn = QPushButton("退出程序")
        self.exit_btn.setMinimumHeight(50)
        self.exit_btn.clicked.connect(self.close)
        
        btn_layout.addWidget(self.course_management_btn)
        btn_layout.addWidget(self.config_btn)
        btn_layout.addWidget(self.exit_btn)
        
        # 状态信息
        status_group = QGroupBox("系统状态")
        status_layout = QVBoxLayout()
        
        self.system_status = QLabel("就绪")
        self.config_status = QLabel(f"配置状态: {'已配置' if os.path.exists('config.ini') else '未配置'}")
        self.course_status = QLabel("当前课程: 未选择")
        
        status_layout.addWidget(self.system_status)
        status_layout.addWidget(self.config_status)
        status_layout.addWidget(self.course_status)
        status_group.setLayout(status_layout)
        
        # 布局组合
        layout.addWidget(title_label)
        layout.addLayout(btn_layout)
        layout.addWidget(status_group)
        
        self.main_tab.setLayout(layout)
    
    def init_monitor_tab(self):
        # 创建滚动区域（右侧带滚动条）
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        
        # 创建内容部件
        content_widget = QWidget()
        layout = QVBoxLayout(content_widget)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        
        # 监控状态
        status_layout = QHBoxLayout()
        
        self.monitor_status_label = QLabel("监控状态: 未运行")
        status_font = QFont()
        status_font.setBold(True)
        self.monitor_status_label.setFont(status_font)
        
        self.current_course_label = QLabel("当前课程: 无")
        
        status_layout.addWidget(self.monitor_status_label)
        status_layout.addWidget(self.current_course_label)
        layout.addLayout(status_layout)
        
        # 统计信息
        stats_group = QGroupBox("监控统计")
        stats_layout = QHBoxLayout()
        
        self.cycle_count = QLabel("总检测周期: 0")
        self.new_page_count = QLabel("新页面检测: 0")
        self.error_count = QLabel("错误次数: 0")
        
        stats_layout.addWidget(self.cycle_count)
        stats_layout.addWidget(self.new_page_count)
        stats_layout.addWidget(self.error_count)
        stats_group.setLayout(stats_layout)
        layout.addWidget(stats_group)
        
        # 日志显示（带滚动条）
        log_group = QGroupBox("监控日志")
        log_layout = QVBoxLayout()
        
        self.log_display = QTextEdit()
        self.log_display.setReadOnly(True)
        self.log_display.setMinimumHeight(300)
        log_layout.addWidget(self.log_display)
        log_group.setLayout(log_layout)
        layout.addWidget(log_group)
        
        # 控制按钮
        btn_layout = QHBoxLayout()
        
        self.start_btn = QPushButton("开始监控")
        self.start_btn.clicked.connect(self.start_monitoring)
        
        self.stop_btn = QPushButton("停止监控")
        self.stop_btn.clicked.connect(self.stop_monitoring)
        self.stop_btn.setEnabled(False)
        
        self.back_btn = QPushButton("返回主菜单")
        self.back_btn.clicked.connect(lambda: self.central_widget.setCurrentIndex(0))
        
        btn_layout.addWidget(self.start_btn)
        btn_layout.addWidget(self.stop_btn)
        btn_layout.addWidget(self.back_btn)
        layout.addLayout(btn_layout)
        
        # 设置滚动区域
        scroll_area.setWidget(content_widget)
        
        # 监控标签页主布局
        main_layout = QVBoxLayout()
        main_layout.addWidget(scroll_area)
        self.monitor_tab.setLayout(main_layout)
    
    def switch_to_main_tab(self):
        self.central_widget.setCurrentIndex(0)
    
    def config_updated(self):
        self.user_config = config_manager.load_config()
        self.config_status.setText("配置状态: 已更新")
    
    def open_course_monitor(self, course_dir, action, course_url=None):
        self.current_course_dir = course_dir
        
        # 获取课程信息
        course_name = os.path.basename(course_dir)
        self.current_course_label.setText(f"当前课程: {course_name}")
        self.course_status.setText(f"当前课程: {course_name}")
        
        # 保存课程URL
        self.current_course_url = course_url
        
        # 切换到监控标签页
        self.central_widget.setCurrentIndex(3)
        self.log_display.clear()
        self.append_log(f"已加载课程: {course_name}")
    
    def start_monitoring(self):
        """启动监控流程，与命令行逻辑一致"""
        if not self.current_course_dir:
            QMessageBox.warning(self, "警告", "请先选择课程")
            return
        
        if self.monitor_thread and self.monitor_thread.isRunning():
            QMessageBox.information(self, "提示", "监控已在运行中")
            return
        
        # 启动登录流程
        self.start_login流程()
    
    def start_login流程(self, use_saved_url=False):
        """启动登录流程，与命令行交互步骤完全一致"""
        # 禁用开始按钮
        self.start_btn.setEnabled(False)
        
        # 创建登录对话框
        self.login_dialog = LoginDialog(self)
        self.login_dialog.setWindowTitle("登录流程")
        
        # 创建登录线程
        server_url = self.user_config['server']['base_url']
        course_url = self.current_course_url if use_saved_url else None
        
        self.login_thread = LoginThread(server_url, course_url)
        
        # 连接信号
        self.login_thread.log_signal.connect(self.login_dialog.append_log)
        self.login_thread.status_signal.connect(self.login_dialog.update_status)
        self.login_thread.login_success.connect(self.on_login_success)
        self.login_thread.login_failed.connect(self.on_login_failed)
        self.login_thread.need_user_confirm.connect(self.handle_login_confirm)
        
        # 连接对话框信号
        self.login_dialog.confirm_scan.connect(self.login_thread.confirm_scan)
        self.login_dialog.confirm_course.connect(self.login_thread.confirm_course_page)
        self.login_dialog.manual_url.connect(self.login_thread.set_manual_url)
        self.login_dialog.rejected.connect(self.cancel_login)
        
        # 启动线程并显示对话框
        self.login_thread.start()
        self.login_dialog.exec()
    
    def handle_login_confirm(self, step):
        """处理登录过程中的用户确认步骤"""
        if step == 1:
            self.login_dialog.enable_scan_confirm()
        elif step == 2:
            self.login_dialog.enable_course_confirm()
    
    def on_login_success(self, driver, course_url):
        """登录成功处理"""
        if self.login_dialog:
            self.login_dialog.accept()
        
        self.driver = driver
        self.current_course_url = course_url
        
        # 保存课程URL到课程信息
        if self.current_course_dir:
            info_path = os.path.join(self.current_course_dir, "course_info.json")
            try:
                course_info = {}
                if os.path.exists(info_path):
                    with open(info_path, "r", encoding="utf-8") as f:
                        course_info = json.load(f)
                
                course_info["url"] = course_url
                course_info["last_opened"] = datetime.datetime.now().isoformat()
                
                with open(info_path, "w", encoding="utf-8") as f:
                    json.dump(course_info, f, ensure_ascii=False, indent=2)
            except Exception as e:
                self.append_log(f"保存课程URL失败: {str(e)}")
        
        self.append_log(f"登录成功，课程URL: {course_url}")
        self.start_actual_monitoring()
    
    def on_login_failed(self, message):
        """登录失败处理"""
        if self.login_dialog:
            self.login_dialog.accept()
        
        self.append_log(f"登录失败: {message}")
        QMessageBox.warning(self, "登录失败", message)
        self.start_btn.setEnabled(True)
        self.login_thread = None
    
    def cancel_login(self):
        """取消登录"""
        if self.login_thread and self.login_thread.isRunning():
            self.login_thread.stop()
        
        self.login_thread = None
        self.start_btn.setEnabled(True)
        self.append_log("登录流程已取消")
    
    def start_actual_monitoring(self):
        """实际启动监控线程"""
        if not self.current_course_url or not self.driver:
            QMessageBox.warning(self, "错误", "未获取到课程URL或浏览器实例")
            self.start_btn.setEnabled(True)
            return
        
        # 创建并启动监控线程
        self.monitor_thread = MonitorThread(
            self.current_course_dir,
            self.current_course_url,
            self.user_config,
            self.driver  # 传递登录线程创建的浏览器实例
        )
        
        # 连接信号
        self.monitor_thread.log_signal.connect(self.append_log)
        self.monitor_thread.status_signal.connect(self.update_monitor_status)
        self.monitor_thread.stats_signal.connect(self.update_stats)
        self.monitor_thread.new_page_signal.connect(self.on_new_page_detected)
        self.monitor_thread.error_occurred.connect(self.show_error)
        
        self.monitor_thread.start()
        
        # 更新UI状态
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.update_monitor_status("监控中")
    
    def stop_monitoring(self):
        if self.monitor_thread and self.monitor_thread.isRunning():
            self.append_log("正在停止监控...")
            self.monitor_thread.stop()
            self.driver = None  # 监控停止后释放浏览器实例
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
        else:
            QMessageBox.information(self, "提示", "监控未在运行")
    
    def append_log(self, text):
        self.log_display.append(text)
        self.log_display.moveCursor(QTextCursor.MoveOperation.End)
    
    def update_monitor_status(self, status):
        self.monitor_status_label.setText(f"监控状态: {status}")
        self.statusBar().showMessage(status)
    
    def update_stats(self, stats):
        self.cycle_count.setText(f"总检测周期: {stats['total_cycles']}")
        self.new_page_count.setText(f"新页面检测: {stats['new_pages_detected']}")
        self.error_count.setText(f"错误次数: {stats['errors_occurred']}")
    
    def on_new_page_detected(self, page_type, page_number):
        page_name_map = {
            "ppt": "PPT",
            "exercise": "选择题",
            "blank": "填空题",
            "subjective": "主观题"
        }
        QMessageBox.information(
            self, 
            "新页面检测", 
            f"检测到新{page_name_map[page_type]}页面\n编号: {page_number}"
        )
    
    def show_error(self, error_msg):
        QMessageBox.warning(self, "监控错误", f"监控过程中发生错误:\n{error_msg}")
    
    def closeEvent(self, event):
        # 停止登录线程
        if self.login_thread and self.login_thread.isRunning():
            self.login_thread.stop()
        
        # 停止监控线程
        if self.monitor_thread and self.monitor_thread.isRunning():
            reply = QMessageBox.question(
                self, 
                "确认退出", 
                "监控正在运行中，确定要退出吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.monitor_thread.stop()
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()


if __name__ == "__main__":
    # 初始化目录
    utils.init_directories()
    
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
