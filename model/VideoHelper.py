from datetime import date
from pathlib import Path

import cv2
import json
import os
import shutil
import numpy as np

from app_paths import APP_ROOT


def load_runtime_options():
    config_path = APP_ROOT / "config.json"
    config = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            config = {}

    video_root_text = str(config.get("video_sorting_station_path") or "").strip()
    video_root = Path(video_root_text) if video_root_text else None
    image_root_text = str(config.get("video_image_root_path") or "").strip()
    if image_root_text:
        image_root = Path(image_root_text)
    else:
        task_root_text = str(config.get("task_path") or "").strip()
        image_root = (
            Path(task_root_text) / date.today().strftime("%m%d")
            if task_root_text
            else None
        )
    try:
        threshold = float(config.get("video_match_ratio_threshold", 0.03))
    except (TypeError, ValueError):
        threshold = 0.03
    return video_root, image_root, min(1.0, max(0.0, threshold))

# 视频后缀
VIDEO_EXTS = ['.mp4', '.mkv', '.avi', '.mov', '.flv', '.wmv', '.ts']
# 图片后缀
IMAGE_EXTS = ['.jpg', '.jpeg', '.png', '.bmp', '.webp']


# ===========================================

def cv_imread(file_path):
    """读取中文路径图片"""
    try:
        img_array = np.fromfile(file_path, dtype=np.uint8)
        img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
        return img
    except Exception:
        return None


def remove_black_borders(img):
    """去除黑边 (Letterbox)"""
    if img is None: return None
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 10, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours: return img
    c = max(contours, key=cv2.contourArea)
    x, y, w, h = cv2.boundingRect(c)
    if w < 50 or h < 50: return img
    return img[y:y + h, x:x + w]


class FeatureMatcher:
    def __init__(self):
        # 初始化 ORB 检测器
        # nfeatures=1000: 提取 1000 个特征点，点越多越精准但越慢
        self.orb = cv2.ORB_create(nfeatures=1000)
        # 初始化暴力匹配器，使用汉明距离
        self.bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)

    def get_features(self, img):
        """计算图片的特征点和描述符"""
        if img is None: return None, None
        # 转换为灰度图计算特征更准
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        keypoints, descriptors = self.orb.detectAndCompute(gray, None)
        return keypoints, descriptors

    def match(self, img_desc, video_desc):
        """
        对比两个描述符集合
        返回: 匹配得分 (0.0 - 1.0)
        """
        if img_desc is None or video_desc is None:
            return 0.0

        if len(img_desc) < 5 or len(video_desc) < 5:
            return 0.0

        try:
            # k=2 表示每个特征点找两个最佳匹配，用于做比例测试
            matches = self.bf.knnMatch(img_desc, video_desc, k=2)

            good_matches = []
            for m, n in matches:
                # Lowe's ratio test: 如果第一个匹配比第二个匹配好很多(距离更短)，才算真的匹配
                # 0.75 是经典参数
                if m.distance < 0.75 * n.distance:
                    good_matches.append(m)

            # 计算得分：成功匹配的数量 / 目标图片的总特征数
            # 逻辑：如果目标图片是视频的一部分（裁剪），那么目标图片的所有特征点都应该在视频里找到。
            score = len(good_matches) / len(img_desc)
            return score
        except Exception:
            return 0.0


    @staticmethod
    def scan_images_recursively(root_dir, matcher):
        image_db = []
        print(f"正在建立图片特征库 (ORB模式): {root_dir} ...")

        count = 0
        for root, dirs, files in os.walk(root_dir):
            for file in files:
                file_path = os.path.join(root, file)
                ext = os.path.splitext(file)[1].lower()

                if ext in IMAGE_EXTS:
                    img = cv_imread(file_path)
                    if img is None: continue

                    # 预先计算并缓存图片的特征，避免重复计算
                    kp, desc = matcher.get_features(img)

                    if desc is not None:
                        image_db.append({
                            'path': file_path,
                            'folder': root,
                            'desc': desc,  # 只存描述符即可
                            'name': file
                        })
                        count += 1

        print(f"特征库建立完成，共 {count} 张图片。")
        return image_db


    @staticmethod
    def get_video_frame_clean(video_path):
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened(): return None

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        # 尝试取稍微靠后一点的帧，因为有时候裁剪的封面选的是精彩片段
        if total_frames > 50:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 30)
        elif total_frames > 20:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 10)

        ret, frame = cap.read()
        cap.release()

        if ret and frame is not None:
            return remove_black_borders(frame)
        return None


def main():
    video_root_dir, image_root_dir, match_ratio_threshold = load_runtime_options()
    if (
        video_root_dir is None
        or image_root_dir is None
        or not video_root_dir.exists()
        or not image_root_dir.exists()
    ):
        print("路径配置错误。")
        return

    # 初始化匹配器
    matcher = FeatureMatcher()

    # 1. 加载图片库 (计算特征)
    img_db = FeatureMatcher.scan_images_recursively(image_root_dir, matcher)
    if not img_db: return

    print(f"开始扫描视频: {video_root_dir} ...")

    processed = 0
    moved = 0

    for root, dirs, files in os.walk(video_root_dir):
        for file in files:
            file_path = os.path.join(root, file)
            ext = os.path.splitext(file)[1].lower()

            if ext in VIDEO_EXTS:
                print(f"正在分析: {file} ...", end='\r')

                # 获取视频帧
                frame = FeatureMatcher.get_video_frame_clean(file_path)
                if frame is None: continue

                # 计算视频帧的特征
                _, video_desc = matcher.get_features(frame)
                if video_desc is None: continue

                best_score = 0.0
                best_match = None

                # 2. 与图片库逐一进行特征匹配
                for img_data in img_db:
                    score = matcher.match(img_data['desc'], video_desc)

                    if score > best_score:
                        best_score = score
                        best_match = img_data

                # 3. 判定匹配
                if best_score >= match_ratio_threshold:
                    target_dir = best_match['folder']

                    if os.path.abspath(root) == os.path.abspath(target_dir):
                        continue

                    target_path = os.path.join(target_dir, file)
                    if os.path.exists(target_path):
                        base, ex = os.path.splitext(file)
                        target_path = os.path.join(target_dir, f"{base}_match{ex}")

                    try:
                        print(f"\n[匹配成功] {file}")
                        print(f"         目标图片: {best_match['name']}")
                        print(f"         特征重合度: {best_score:.2%}")  # 显示百分比

                        shutil.move(file_path, target_path)
                        moved += 1
                    except Exception as e:
                        print(f"\n[错误] {e}")

                processed += 1

    print("\n" + "=" * 30)
    print(f"处理完成。归类: {moved}/{processed}")


if __name__ == "__main__":
    main()
