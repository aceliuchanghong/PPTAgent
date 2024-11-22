import json
import os
import tempfile
import traceback
from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial

import jsonlines
import PIL.Image
import torch
from FlagEmbedding import BGEM3FlagModel
from jinja2 import Environment, StrictUndefined
from rich import print

from apis import API_TYPES, CodeExecutor
from llms import Role
from model_utils import get_text_embedding
from presentation import Presentation, SlidePage, StyleArg
from utils import (
    Config,
    get_slide_content,
    pexists,
    pjoin,
    ppt_to_images,
    prepare_shape_label,
    tenacity,
)


@dataclass
class PPTGen(ABC):
    roles: list[str] = field(default_factory=list)

    def __init__(
        self,
        text_model: BGEM3FlagModel,
        retry_times: int = 3,
        force_pages: bool = False,
        error_exit: bool = True,
        record_cost: bool = True,
        **kwargs,
    ):
        self.text_model = text_model
        self.retry_times = retry_times
        self.force_pages = force_pages
        self.error_exit = error_exit
        self.staffs: dict[str, Role] = self._hire_staffs(record_cost, **kwargs)

    def set_examplar(
        self,
        presentation: Presentation,
        slide_induction: dict,
    ):
        self.presentation = presentation
        self.slide_induction = slide_induction
        self.functional_keys = slide_induction.pop("functional_keys")
        self.layout_names = list(slide_induction.keys())
        self.layout_embeddings = torch.stack(
            get_text_embedding(self.layout_names, self.text_model)
        )
        self.empty_prs = deepcopy(presentation)
        return self

    def generate_pres(
        self,
        config: Config,
        images: dict[str, str],
        num_slides: int,
        doc_json: dict[str, str],
    ):
        self.config = config
        self.doc_json = doc_json
        meta_data = "\n".join(
            [f"{k}: {v}" for k, v in self.doc_json.get("metadata", {}).items()]
        )
        self.metadata = (
            f"{meta_data}\nCurrent Time: {datetime.now().strftime('%Y-%m-%d')}\n"
        )
        self.image_information = ""
        for k, v in images.items():
            assert pexists(k), f"Image {k} not found"
            size = PIL.Image.open(k).size
            self.image_information += (
                f"Image path: {k}, size: {size[0]}*{size[1]} px\n caption: {v}\n"
            )
        succ_flag = True
        code_executor = CodeExecutor(self.retry_times)
        self.outline = self._generate_outline(num_slides)
        self.simple_outline = "\n".join(
            [
                f"Slide {slide_idx+1}: {slide_title}"
                for slide_idx, slide_title in enumerate(self.outline)
            ]
        )
        generated_slides = []
        for slide_data in enumerate(self.outline.items()):
            if self.force_pages and slide_data[0] == num_slides:
                break
            slide = self._generate_slide(slide_data, code_executor)
            if slide is not None:
                generated_slides.append(slide)
                continue
            if self.config.DEBUG:
                traceback.print_exc()
            if self.error_exit:
                succ_flag = False
                break
        self._save_history(code_executor)
        if succ_flag:
            self.empty_prs.slides = generated_slides
            self.empty_prs.save(pjoin(self.config.RUN_DIR, "final.pptx"))

    def _save_history(self, code_executor: CodeExecutor):
        os.makedirs(pjoin(self.config.RUN_DIR, "history"), exist_ok=True)
        for role in self.staffs.values():
            role.save_history(pjoin(self.config.RUN_DIR, "history"))
            role.history = []
        with jsonlines.open(
            pjoin(self.config.RUN_DIR, "code_steps.jsonl"), "w"
        ) as writer:
            writer.write_all(code_executor.code_history)
        with jsonlines.open(
            pjoin(self.config.RUN_DIR, "agent_steps.jsonl"), "w"
        ) as writer:
            writer.write_all(code_executor.api_history)

    @tenacity
    def _generate_outline(self, num_slides: int):
        outline_file = pjoin(self.config.RUN_DIR, "presentation_outline.json")
        if pexists(outline_file):
            outline = json.load(open(outline_file, "r"))
        else:
            outline = self.staffs["planner"](
                num_slides=num_slides,
                layouts="\n".join(
                    set(self.slide_induction.keys()).difference(self.functional_keys)
                ),
                functional_keys="\n".join(self.functional_keys),
                json_content=self.doc_json,
                image_information=self.image_information,
            )
            outline = self._valid_outline(outline)
            json.dump(
                outline,
                open(outline_file, "w"),
                ensure_ascii=False,
                indent=4,
            )
        return outline

    def _valid_outline(self, outline: dict, retry: int = 0) -> dict:
        try:
            for slide in outline.values():
                layout_sim = torch.cosine_similarity(
                    get_text_embedding(slide["layout"], self.text_model),
                    self.layout_embeddings,
                )
                if layout_sim.max() < 0.7:
                    raise ValueError(
                        f"Layout `{slide['layout']}` not found, must be one of {self.layout_names}"
                    )
                slide["layout"] = self.layout_names[layout_sim.argmax().item()]
            if any(
                not {"layout", "subsection_keys", "description"}.issubset(
                    set(slide.keys())
                )
                for slide in outline.values()
            ):
                raise ValueError(
                    "Invalid outline structure, must be a dict with layout, subsection_keys, description"
                )
        except ValueError as e:
            if retry < self.retry_times:
                new_outline = self.staffs["planner"].retry(
                    str(e), traceback.format_exc(), retry + 1
                )
                return self._valid_outline(new_outline, retry + 1)
            else:
                raise ValueError("Failed to generate outline, tried too many times")
        return outline

    def _hire_staffs(self, record_cost: bool, **kwargs) -> dict[str, Role]:
        jinja_env = Environment(undefined=StrictUndefined)
        return {
            role: Role(
                role,
                env=jinja_env,
                record_cost=record_cost,
                text_model=self.text_model,
                **kwargs,
            )
            for role in ["planner"] + self.roles
        }

    @abstractmethod
    def synergize(
        self,
        template: dict,
        slide_content: str,
        code_executor: CodeExecutor,
        image_info: str,
    ) -> SlidePage:
        pass

    def _generate_slide(self, slide_data, code_executor: CodeExecutor) -> SlidePage:
        slide_idx, (slide_title, slide) = slide_data
        image_info = "No Images"
        if any(
            [
                i in slide["layout"]
                for i in ["picture", "chart", "table", "diagram", "freeform"]
            ]
        ):
            image_info = self.image_information
        slide_content = f"Slide-{slide_idx+1} " + get_slide_content(
            self.doc_json, slide_title, slide
        )
        template = deepcopy(self.slide_induction[slide["layout"]])
        try:
            return self.synergize(
                template,
                slide_content,
                code_executor,
                image_info,
            )
        except Exception as e:
            if self.config.DEBUG:
                print(f"generate slide {slide_idx} failed: {e}")
            return None


class PPTAgent(PPTGen):
    roles: list[str] = ["agent"]

    def synergize(
        self,
        template: dict,
        slide_content: str,
        code_executor: CodeExecutor,
        image_info: str,
    ) -> SlidePage:
        slide = deepcopy(self.presentation.slides[template["template_id"] - 1])
        actions = self.staffs["agent"](
            api_documentation=code_executor.get_apis_docs(API_TYPES.Agent.value),
            edit_target=slide.html,
            content=self.simple_outline + self.metadata + slide_content,
            image_information=image_info,
        )
        code_executor.execute_actions(
            actions=actions,
            edit_slide=slide,
        )
        return slide


class PPTCrew(PPTGen):
    roles: list[str] = ["editor", "coder"]  # , "typographer"]

    # 还是把它加上
    def synergize(
        self,
        template: dict,
        slide_content: str,
        code_executor: CodeExecutor,
        images_info: str,
    ) -> SlidePage:
        content_schema = template["content_schema"]
        old_data = self._prepare_schema(content_schema)
        editor_output = self.staffs["editor"](
            schema=content_schema,
            outline=self.simple_outline,
            metadata=self.metadata,
            text=slide_content,
            images_info=images_info,
        )
        command_list = self._generate_commands(editor_output, content_schema, old_data)
        edit_actions = self.staffs["coder"](
            api_docs=code_executor.get_apis_docs(API_TYPES.Agent.value),
            edit_target=self.presentation.slides[template["template_id"] - 1].to_html(),
            command_list="\n".join([str(i) for i in command_list]),
        )
        for error_idx in range(self.retry_times):
            slide: SlidePage = deepcopy(
                self.presentation.slides[template["template_id"] - 1]
            )
            feedback = code_executor.execute_actions(edit_actions, slide)
            if feedback is None:
                break
            if error_idx == self.retry_times - 1:
                raise Exception("Failed to generate slide, tried too many times")
            edit_actions = self.staffs["coder"].retry(*feedback, error_idx + 1)

        if "typographer" in self.staffs:
            typography_actions = self.staffs["typographer"](
                api_docs=code_executor.get_apis_docs(API_TYPES.Typographer.value),
                edit_target=slide.to_html(StyleArg(geometry=True, size=True)),
                slide_image=self._prepare_slide_image(slide),
            )
            for error_idx in range(self.retry_times):
                edited_slide = deepcopy(slide)
                feedback = code_executor.execute_actions(
                    typography_actions, edited_slide
                )
                if feedback is None:
                    return edited_slide
                if error_idx == self.retry_times - 1:
                    return slide
                edit_actions = self.staffs["typographer"].retry(
                    *feedback, error_idx + 1
                )

        return slide

    def _prepare_slide_image(self, slide: SlidePage):
        temp_dir = tempfile.TemporaryDirectory()
        self.empty_prs.clear_slides()
        slide = self.empty_prs.prs.slides[0]
        for shape in slide:
            shape.closures["style"].append(
                partial(prepare_shape_label, shape.shape_idx)
            )
        self.empty_prs.build_slide(slide)
        self.empty_prs.save(temp_dir.name + "/temp.pptx")
        ppt_to_images(temp_dir.name + "/temp.pptx", temp_dir.name)
        assert len(os.listdir(temp_dir.name)) == 2
        return temp_dir.name + "/slide_0001.jpg"

    def _prepare_schema(self, content_schema: dict):
        old_data = {}
        for el_name, el_info in content_schema.items():
            if el_info["type"] == "text":
                if not isinstance(el_info["data"], list):
                    el_info["data"] = [el_info["data"]]
                if len(el_info["data"]) > 1:
                    charater_counts = [len(i) for i in el_info["data"]]
                    content_schema[el_name]["suggestedCharacters"] = (
                        str(min(charater_counts)) + "-" + str(max(charater_counts))
                    )
                else:
                    content_schema[el_name]["suggestedCharacters"] = "<" + str(
                        len(el_info["data"][0])
                    )
            old_data[el_name] = el_info.pop("data")
            content_schema[el_name]["default_quantity"] = 1
            if isinstance(old_data[el_name], list):
                content_schema[el_name]["default_quantity"] = len(old_data[el_name])
        return old_data

    def _generate_commands(
        self, editor_output: dict, content_schema: dict, old_data: dict
    ):
        command_list = []
        for el_info in editor_output.values():
            assert "data" in el_info, "data not found in editor_output"

        for el_name, old_content in old_data.items():
            # 教会它不要生成不该生成的
            if not isinstance(old_content, list):
                old_content = [old_content]

            new_content = editor_output.get(el_name, {}).get("data", None)
            if not isinstance(new_content, list):
                new_content = [new_content]

            if content_schema[el_name]["type"] == "image":
                for i in range(len(new_content)):
                    if not pexists(new_content[i]):
                        new_content[i] = None
            new_content = [i for i in new_content if i]

            quantity_change = len(new_content) - len(old_content)
            command_list.append(
                (
                    el_name,
                    content_schema[el_name]["type"],
                    f"quantity_change: {quantity_change}",
                    old_content,
                    new_content,
                )
            )

        return command_list
