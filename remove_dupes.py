import os
import cv2
import time
import sqlite3
import requests
from pathlib import Path
from user_config import (
    flask_server,
    database_path,
    blacklisted_phash_path,
    mse_image_threshold,
    mse_video_threshold,
    min_group_size,
)

# currently only works for videos!
# once i get a better solution at adding the image phashes to the database, we can use this to remove duplicate images as well
# the is_frames_match should already work for images, when that is implemented

OUTPUT_TO_FLASK = (
    True  # This isn't handled by this script, but by the standalone flask server
)


def is_server_live():
    try:
        response = requests.get(f"{flask_server}/health-check")
        return response.status_code == 200
    except requests.exceptions.ConnectionError:
        return False


def update_videos(video1_path, video2_path, phash):
    video1_name = Path(video1_path).name
    video2_name = Path(video2_path).name

    try:
        response = requests.get(
            f"{flask_server}/update",
            params={
                "video1_path": video1_path,
                "video2_path": video2_path,
                "video1_name": video1_name,
                "video2_name": video2_name,
                "phash": phash,
            },
        )
        if response.status_code == 200:
            print("Videos updated successfully")
        else:
            print("Failed to update videos")
    except requests.exceptions.RequestException as e:
        print(e)


def file_to_list(file_path):
    with open(file_path, "r") as f:
        return [line.strip() for line in f]


class pHashProcessor:
    def __init__(self):
        self.BLACKLISTED_PHASHES = file_to_list(blacklisted_phash_path)

    def connect_to_database(self, database_path):
        conn = sqlite3.connect(database_path)
        return conn

    def disconnect_from_database(self, conn):
        conn.close()

    def read_rows_with_phash(self, conn):
        cursor = conn.cursor()
        cursor.execute(
            "SELECT file_id, file_model, file_basename, file_parent, file_size, phash, duration, video_codec, audio_codec, video_format, width, height, bit_rate, frame_rate FROM files WHERE phash IS NOT NULL"
        )
        rows = cursor.fetchall()
        return rows

    def build_dict_from_rows(self, rows):
        column_names = [
            "file_id",
            "file_model",
            "file_basename",
            "file_parent",
            "file_size",
            "phash",
            "duration",
            "video_codec",
            "audio_codec",
            "video_format",
            "width",
            "height",
            "bit_rate",
            "frame_rate",
        ]
        result = []
        for row in rows:
            item = {}
            for i, value in enumerate(row):
                if value is None or value == "":
                    item[column_names[i]] = None
                else:
                    item[column_names[i]] = value
            item["file_path"] = item["file_parent"] + "/" + item["file_basename"]
            result.append(item)
        return result

    def get_curated_grouped_entries(
        self, result_dict, min_size=None, min_duration=None
    ):
        # Create curated grouped_entries with only groups containing more than one entry
        curated_grouped_entries = {
            phash: group for phash, group in result_dict.items() if len(group) > 1
        }

        # Filter out entries with non-existent file paths
        curated_grouped_entries = {
            phash: [entry for entry in group if os.path.exists(entry["file_path"])]
            for phash, group in curated_grouped_entries.items()
        }

        # Remove groups with only one entry
        curated_grouped_entries = {
            phash: group
            for phash, group in curated_grouped_entries.items()
            if len(group) > 1
        }

        # if min_size, filter out groups with total size less than min_size
        if min_size is not None:
            curated_grouped_entries = {
                phash: group
                for phash, group in curated_grouped_entries.items()
                if sum(entry["file_size"] for entry in group) >= min_size
            }

        # if min_duration, filter out groups with total duration less than min_duration
        if min_duration is not None:
            curated_grouped_entries = {
                phash: group
                for phash, group in curated_grouped_entries.items()
                if round(sum(entry["duration"] for entry in group)) >= min_duration
            }

        return curated_grouped_entries

    def group_by_phash(self, entries, exact_match=True, blacklisted_phashes=[]):
        if exact_match:
            groups = {}
            for entry in entries:
                phash = entry["phash"]
                if entry["phash"] not in blacklisted_phashes:
                    if phash in groups:
                        groups[phash].append(entry)
                    else:
                        groups[phash] = [entry]
            return groups

    def process_grouped_entries(self, grouped_entries, auto_delete=False):
        # print how many groups exist in curated_grouped_entries
        print(f"Number of groups: {len(grouped_entries)}\n")

        # Calculate summed file size for each group
        group_sizes = {
            phash: sum(entry["file_size"] for entry in group)
            for phash, group in grouped_entries.items()
        }

        # Sort groups by summed file size in descending order
        sorted_groups = sorted(
            grouped_entries.values(),
            key=lambda group: group_sizes[group[0]["phash"]],
            reverse=True,
        )

        for group in sorted_groups:
            phash_value = group[0]["phash"]
            print(
                f"Group - phash: {phash_value} (Summed File Size: {self.readable_size(group_sizes[phash_value])})"
            )

            # Process the group
            self.process_delete_files(group, auto_delete=auto_delete)

    def sort_files_by_size(self, group):
        return sorted(group, key=lambda entry: entry["file_size"], reverse=True)

    def separate_premium_and_non_premium_files(self, sorted_files):
        premium_files = []
        non_premium_files = []
        for entry in sorted_files:
            file_path = Path(entry["file_path"])
            if file_path.parent.name == "premium":
                premium_files.append(entry)
            else:
                non_premium_files.append(entry)
        return premium_files, non_premium_files

    def find_biggest_file(self, group, premium_files):
        biggest_file = None
        for entry in group:
            if biggest_file is None or entry["file_size"] > biggest_file["file_size"]:
                biggest_file = entry
            elif entry["file_size"] == biggest_file["file_size"]:
                if entry in premium_files:
                    biggest_file = entry
        return biggest_file

    def process_group(self, group, auto_delete=False):
        sorted_files = self.sort_files_by_size(group)
        premium_files, non_premium_files = self.separate_premium_and_non_premium_files(
            sorted_files
        )

        if auto_delete:
            if not self.is_frames_match(
                [entry["file_path"] for entry in premium_files + non_premium_files]
            ):
                print("In auto-delete mode.")
                print("Frames do not match. Skipping group.\n")
                return

        biggest_file = self.find_biggest_file(group, premium_files)
        all_same_model = len(set(entry["file_model"] for entry in group)) == 1

        if all_same_model:
            self.process_same_model_files(
                biggest_file, premium_files, non_premium_files, auto_delete
            )
        else:
            self.process_different_model_files(
                group, biggest_file, premium_files, non_premium_files, auto_delete
            )

    def process_same_model_files(
        self, biggest_file, premium_files, non_premium_files, auto_delete
    ):
        for i, entry in enumerate(premium_files + non_premium_files, start=1):
            if entry != biggest_file:
                frames_match = self.is_frames_match(
                    [entry["file_path"], biggest_file["file_path"]]
                )

                if OUTPUT_TO_FLASK:
                    if is_server_live():
                        video1_path = biggest_file["file_path"]
                        video2_path = entry["file_path"]
                        update_videos(video1_path, video2_path, entry["phash"])

                print("Biggest file:")
                print(f"File Model: {biggest_file['file_model']}")
                print(f"File Path: {biggest_file['file_path']}")
                print(f"File Size: {self.readable_size(biggest_file['file_size'])}")
                print(
                    f"File Duration: {self.readable_duration(biggest_file['duration'])}\n"
                )

                print("Ready to delete:")
                print(f"Frames Match: {frames_match}")
                print(f"File Model: {entry['file_model']}")
                print(f"File Path: {entry['file_path']}")
                print(f"File Size: {self.readable_size(entry['file_size'])}")
                print(f"File Duration: {self.readable_duration(entry['duration'])}\n")

                if i < len(non_premium_files):
                    print(f"File [{i} of {len(non_premium_files)}]")

                if auto_delete:
                    if frames_match:
                        self.remove_file(Path(entry["file_path"]))
                        print()
                else:
                    user_choice = input("Do you want to delete this file? (y/n): ")
                    if user_choice.lower() != "n":
                        self.remove_file(Path(entry["file_path"]))
                        print()

    def process_different_model_files(
        self, group, biggest_file, premium_files, non_premium_files, auto_delete
    ):
        if not self.is_frames_match(
            [entry["file_path"] for entry in premium_files + non_premium_files]
        ):
            print("Frames do not match for all files in group. Be weary!\n")

        file_models = set(entry["file_model"] for entry in group)

        for file_model in file_models:
            files_with_model = [
                entry for entry in group if entry["file_model"] == file_model
            ]
            biggest_file_with_model = max(
                files_with_model, key=lambda entry: entry["file_size"]
            )

            frames_match = self.is_frames_match(
                [biggest_file_with_model["file_path"], biggest_file["file_path"]]
            )

            # get the phash for the biggest file with the model

            if OUTPUT_TO_FLASK:
                if is_server_live():
                    video1_path = str(biggest_file["file_path"])
                    video2_path = str(biggest_file_with_model["file_path"])
                    update_videos(
                        video1_path, video2_path, biggest_file_with_model["phash"]
                    )

            print(f"Frames Match: {frames_match}")
            print(f"File Model: {file_model}")
            print(f"Biggest File Path: {biggest_file_with_model['file_path']}")
            print(
                f"File Size: {self.readable_size(biggest_file_with_model['file_size'])}"
            )
            print(
                f"File Duration: {self.readable_duration(biggest_file_with_model['duration'])}\n"
            )

        chosen_model = input("Enter the file model you want to preserve: ")
        print()

        for entry in group:
            if entry["file_model"] != chosen_model:
                frames_match = self.is_frames_match(
                    [entry["file_path"], biggest_file["file_path"]]
                )
                print("Ready to delete:")
                print(f"Frames Match: {frames_match}")
                print(f"File Model: {entry['file_model']}")
                print(f"File Path: {entry['file_path']}")
                print(f"File Size: {self.readable_size(entry['file_size'])}")
                print(f"File Duration: {self.readable_duration(entry['duration'])}\n")

                if OUTPUT_TO_FLASK:
                    if is_server_live():
                        video1_path = biggest_file["file_path"]
                        video2_path = entry["file_path"]
                        update_videos(video1_path, video2_path, entry["phash"])

                if auto_delete:
                    if frames_match:
                        self.remove_file(Path(entry["file_path"]))
                        print()
                else:
                    user_choice = input("Do you want to delete this file? (y/n): ")
                    if user_choice.lower() != "n":
                        self.remove_file(Path(entry["file_path"]))
                        print()

    def process_delete_files(self, group, auto_delete=False):
        self.process_group(group, auto_delete)

    def is_frames_match(self, file_paths):
        def is_video(path):
            # Check if the given file path corresponds to a video file
            return cv2.VideoCapture(path).isOpened()

        def is_image(path):
            # Check if the given file path corresponds to an image file
            return cv2.imread(path) is not None

        def is_video_frames_match(video_paths):
            try:
                # Read the first frame from the first video
                cap = cv2.VideoCapture(video_paths[0])
                ret, frame1 = cap.read()
                cap.release()

                # Iterate through the rest of the video paths
                for path in video_paths[1:]:
                    # Read the first frame from the current video
                    cap = cv2.VideoCapture(path)
                    ret, frame2 = cap.read()
                    cap.release()

                    # Check if frames could not be read
                    if frame1 is None or frame2 is None:
                        return False

                    # Resize the frames if they have different sizes
                    if frame1.shape != frame2.shape:
                        frame1 = cv2.resize(frame1, frame2.shape[:2][::-1])

                    # Compare the frames using mean squared error (MSE)
                    mse = ((frame1 - frame2) ** 2).mean()

                    # Check if the frames are roughly the same
                    if mse > mse_video_threshold:
                        return False

                return True

            except Exception as e:
                print(f"An error occurred: {str(e)}")
                return False

        def is_image_frames_match(image_paths):
            try:
                # Read the first image
                frame1 = cv2.imread(image_paths[0])

                # Iterate through the rest of the image paths
                for path in image_paths[1:]:
                    # Read the current image
                    frame2 = cv2.imread(path)

                    # Check if images could not be read
                    if frame1 is None or frame2 is None:
                        return False

                    # Resize the images if they have different sizes
                    if frame1.shape != frame2.shape:
                        frame1 = cv2.resize(frame1, frame2.shape[:2][::-1])

                    # Compare the images using mean squared error (MSE)
                    mse = ((frame1 - frame2) ** 2).mean()

                    # Check if the images are roughly the same
                    if mse > mse_image_threshold:
                        return False

                return True

            except Exception as e:
                print(f"An error occurred: {str(e)}")
                return False

        try:
            # Check if the input paths correspond to videos
            if all(is_video(path) for path in file_paths):
                return is_video_frames_match(file_paths)

            # Check if the input paths correspond to images
            if all(is_image(path) for path in file_paths):
                return is_image_frames_match(file_paths)

            # Unsupported input type
            print("Unsupported input type")
            return False

        except Exception as e:
            print(f"An error occurred: {str(e)}")
            return False

    def readable_size(self, bytes):
        if bytes < 1024:
            return f"{bytes} B"
        elif bytes < 1024**2:
            return f"{bytes / 1024:.2f} KB"
        elif bytes < 1024**3:
            return f"{bytes / 1024 ** 2:.2f} MB"
        elif bytes < 1024**4:
            return f"{bytes / 1024 ** 3:.2f} GB"
        else:
            return f"{bytes / 1024 ** 4:.2f} TB"

    def readable_duration(self, float):
        seconds = int(float)

        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        seconds = seconds % 60

        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def remove_file(self, file_path):
        path = Path(file_path)
        if path.exists():
            while path.exists():
                try:
                    path.unlink()
                    print(f"Deleted file: {path}")
                except PermissionError:
                    print(f"Error attempting to delete {path.name}.")
                    time.sleep(1)

    def move_file(self, file_path, destination):
        source_path = Path(file_path)
        destination_path = Path(destination)
        if source_path.exists():
            try:
                destination_path.parent.mkdir(parents=True, exist_ok=True)
                source_path.rename(destination_path)
                print(f"Moved file: {source_path} to {destination_path}")
            except OSError as e:
                print(f"Error: {e}")


def main():
    processor = pHashProcessor()
    conn = processor.connect_to_database(database_path)
    rows = processor.read_rows_with_phash(conn)
    result_dict = processor.build_dict_from_rows(rows)
    processor.disconnect_from_database(conn)

    grouped_entries = processor.group_by_phash(
        result_dict, blacklisted_phashes=processor.BLACKLISTED_PHASHES
    )

    curated_grouped_entries = processor.get_curated_grouped_entries(
        grouped_entries, min_size=min_group_size
    )

    os.system("cls")
    processor.process_grouped_entries(curated_grouped_entries, auto_delete=False)

    print()
    input("Press Enter to exit...")


if __name__ == "__main__":
    main()
