"""Repair the latest itinerary, retaining real quotes and explicit selections."""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import plan


class AuditRepairTests(unittest.TestCase):
    def setUp(self):
        self.original={"revision":4,"destination":"杭州","start_date":"2026-10-10","end_date":"2026-10-10",
          "basic":{"travelers":"2人","total_budget":4000},"blocks":[
            {"id":"s","name":"博物馆","type":"景点","plan_style":"经典","day":1,"date":"2026-10-10","time":"09:00-12:30","unit_price":30,"price":60,"price_known":True},
            {"id":"m","name":"杭帮菜店","type":"美食","meal":"午餐","plan_style":"经典","day":1,"date":"2026-10-10","time":"12:00-13:00","user_selected":True,"unit_price":80,"price":160,"price_known":True},
            {"id":"o","name":"公园","type":"景点","plan_style":"轻松","day":1,"date":"2026-10-10","time":"09:00-11:00","price":0,"unit_price":None,"price_known":False}]}
        self.issues=[{"severity":"high","type":"时间","detail":"博物馆和午餐时间重叠","suggestion":"提前结束游览",
                      "actionable":True,"plan_style":"经典","day":1,"block_ids":["s","m"]}]

    def test_real_modify_preserves_selected_meal_prices_other_style_and_hard_budget(self):
        original=copy.deepcopy(self.original)
        candidate=plan.rebuild_itineraries(copy.deepcopy(original))
        schedule=candidate["plans"][0]["itinerary"][0]["schedule"]
        schedule[0].update(time="09:00-11:30",price=0,unit_price=0)
        schedule[1].update(name="模型乱换的餐厅",price=1,unit_price=1)
        candidate["plans"][1]["itinerary"][0]["schedule"][0]["name"]="模型乱换的公园"
        candidate.update(destination="上海",start_date="2026-11-10",end_date="2026-11-20")
        resp=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(candidate)))])
        with patch.object(plan,"OpenAI"),patch.object(plan,"_chat_completion",return_value=resp) as model,              patch.object(plan,"finalize_plan",side_effect=lambda value,*a,**kw:value) as finalize:
            result=plan.repair_plan(original,self.issues,{"destination":"杭州"},basic=original["basic"])
        blocks={b["id"]:b for b in result["blocks"]}
        self.assertEqual(blocks["s"]["time"],"09:00-11:30")
        self.assertEqual(blocks["s"]["unit_price"],30)
        self.assertEqual(blocks["m"]["name"],"杭帮菜店")
        self.assertEqual(blocks["m"]["unit_price"],80)
        self.assertTrue(blocks["m"]["user_selected"])
        self.assertEqual({key:blocks["o"][key] for key in original["blocks"][2]},original["blocks"][2])
        self.assertEqual(result["destination"],"杭州")
        self.assertEqual(result["start_date"],"2026-10-10")
        self.assertEqual(result["end_date"],"2026-10-10")
        self.assertEqual(result["basic"],original["basic"])
        sent=json.loads(model.call_args.kwargs["messages"][1]["content"])
        self.assertIn("时间重叠",sent["modify"]["instruction"])
        self.assertEqual(finalize.call_args.kwargs["refresh_food_targets"],[{"plan_style":"经典","day":1}])
        self.assertEqual(original,self.original)

    def test_warnings_do_not_initialize_model(self):
        with patch.object(plan,"OpenAI") as model:
            result=plan.repair_plan(self.original,[{**self.issues[0],"severity":"medium"}])
        self.assertIn("error",result)
        model.assert_not_called()

    def test_incomplete_replacement_does_not_drop_original_style(self):
        with patch.object(plan,"modify_plan",return_value={"plans":[]}),patch.object(plan,"finalize_plan") as finalize:
            result=plan.repair_plan(self.original,self.issues,{"destination":"杭州"})
        self.assertIn("error",result)
        finalize.assert_not_called()
        self.assertEqual(len(self.original["blocks"]),3)

    def compact_reply(self, time="09:00-11:00"):
        candidate = plan.rebuild_itineraries(copy.deepcopy(self.original))
        candidate["plans"][0]["itinerary"][0]["schedule"][0]["time"] = time
        return {"plans": [{"style": style["style"], "itinerary": [
            {"day": day["day"], "schedule": [
                {key: item[key] for key in ("id", "name", "type", "time", "note") if key in item}
                for item in day["schedule"]]} for day in style["itinerary"]]}
            for style in candidate["plans"]]}

    @staticmethod
    def response(content, finish_reason="stop"):
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish_reason,
                               message=SimpleNamespace(content=content))])

    def repair_with_responses(self, responses, search=None):
        with patch.object(plan, "OpenAI"), patch.object(plan, "_chat_completion", side_effect=responses) as model, \
             patch.object(plan, "finalize_plan", side_effect=lambda value, *a, **kw: value) as finalize:
            result = plan.repair_plan(self.original, self.issues, search or {"destination": "杭州"})
        return result, model, finalize

    def test_long_invalid_json_is_regenerated_without_copying_invalid_output(self):
        broken = '{"unneeded":"' + 'x' * 26600 + '","plans": }'
        result, model, finalize = self.repair_with_responses([
            self.response(broken), self.response(json.dumps(self.compact_reply()))])
        self.assertNotIn("error", result)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(result["blocks"][0]["time"], "09:00-11:00")
        retry = model.call_args.kwargs["messages"]
        self.assertIn("JSON 格式无效", retry[-1]["content"])
        self.assertNotIn('x' * 100, json.dumps(retry))
        finalize.assert_called_once()
        self.assertEqual(self.original["blocks"][0]["time"], "09:00-12:30")

    def test_length_finish_is_retried_even_if_content_is_parseable(self):
        first = self.compact_reply("09:00-12:30")
        result, model, _ = self.repair_with_responses([
            self.response(json.dumps(first), "length"), self.response(json.dumps(self.compact_reply()))])
        self.assertEqual(model.call_count, 2)
        self.assertIn("回复被截断", model.call_args.kwargs["messages"][-1]["content"])
        self.assertEqual(result["blocks"][0]["time"], "09:00-11:00")

    def test_service_body_json_error_is_also_regenerated(self):
        result, model, _ = self.repair_with_responses([
            json.JSONDecodeError("Expecting value", "", 0), self.response(json.dumps(self.compact_reply()))])
        self.assertNotIn("error", result)
        self.assertEqual(model.call_count, 2)
        self.assertIn("规划服务返回的 JSON 格式无效", model.call_args.kwargs["messages"][-1]["content"])

    def test_two_truncated_replies_keep_original_and_return_readable_error(self):
        original = copy.deepcopy(self.original)
        result, model, finalize = self.repair_with_responses([
            self.response('{"plans":[', "length"), self.response('{"plans":[', "length")])
        self.assertIn("回复被截断", result["error"])
        self.assertIn("原行程已保留", result["error"])
        self.assertEqual(model.call_count, 2)
        finalize.assert_not_called()
        self.assertEqual(self.original, original)

    def test_invalid_empty_incomplete_or_duplicate_reply_never_replaces_plan(self):
        missing_style = self.compact_reply()
        missing_style["plans"].pop()
        missing_day = self.compact_reply()
        missing_day["plans"][0]["itinerary"] = []
        wrong_schedule = self.compact_reply()
        wrong_schedule["plans"][0]["itinerary"][0]["schedule"] = ["不是活动对象"]
        duplicate_id = self.compact_reply()
        duplicate_id["plans"][0]["itinerary"][0]["schedule"][1]["id"] = "s"
        bad_replies = ["", None, '{"plans":...}', '{"plans":NaN}', '{"plans":[]}',
                       json.dumps(missing_style), json.dumps(missing_day), json.dumps(wrong_schedule), json.dumps(duplicate_id)]
        for content in bad_replies:
            with self.subTest(content=str(content)[:60]):
                original = copy.deepcopy(self.original)
                result, model, finalize = self.repair_with_responses([self.response(content), self.response(content)])
                self.assertIn("原行程已保留", result["error"])
                self.assertNotIn("Expecting value", result["error"])
                self.assertEqual(model.call_count, 2)
                finalize.assert_not_called()
                self.assertEqual(self.original, original)

    def test_compact_reply_restores_links_options_coordinates_and_day_metadata(self):
        spot, meal = self.original["blocks"][:2]
        spot.update(link="https://example.test/spot/" + 'z' * 10000, lng=120.1, lat=30.2,
                    activity_window={"start":"09:00", "end":"20:00"})
        meal.update(link="https://example.test/meal", selected_option="杭帮菜店",
                    options=[{"name":"杭帮菜店", "price":80, "lng":120.11, "lat":30.21}])
        self.original.update(legs=[{"polyline":'z' * 10000}], food_by_anchor=[{"options":'z' * 10000}])
        original = copy.deepcopy(self.original)
        reply = self.compact_reply()
        # Model metadata is ignored; the existing source records are authoritative.
        reply["plans"][0]["itinerary"][0]["schedule"][0].update(price=1, lng=999, link="model-invented")
        result, model, _ = self.repair_with_responses([self.response("```json\n" + json.dumps(reply) + "\n```")])
        sent = json.loads(model.call_args.kwargs["messages"][1]["content"])["plan"]
        self.assertNotIn("legs", sent)
        self.assertNotIn("food_by_anchor", sent)
        sent_spot = sent["plans"][0]["itinerary"][0]["schedule"][0]
        self.assertNotIn("link", sent_spot)
        self.assertNotIn("options", sent["plans"][0]["itinerary"][0]["schedule"][1])
        blocks = {item["id"]: item for item in result["blocks"]}
        for key in ("link", "lng", "lat", "activity_window", "unit_price", "price_known"):
            self.assertEqual(blocks["s"][key], original["blocks"][0][key])
        for key in ("link", "options", "selected_option", "unit_price", "user_selected"):
            self.assertEqual(blocks["m"][key], original["blocks"][1][key])
        self.assertEqual(result["plans"][0]["itinerary"][0]["activity_window"], spot["activity_window"])
        self.assertEqual(self.original, original)

    def test_replacement_place_cannot_inherit_previous_geo_or_invented_quote(self):
        self.original["blocks"][0].update(lng=120.1, lat=30.2, link="https://example.test/old")
        reply = self.compact_reply()
        reply["plans"][0]["itinerary"][0]["schedule"][0].update(name="新的真实景点", price=1, lng=999, link="model-invented")
        result, _, _ = self.repair_with_responses([self.response(json.dumps(reply))],
                            {"destination":"杭州", "poi":[{"name":"新的真实景点", "price":90}]})
        changed = next(item for item in result["blocks"] if item["id"] == "s")
        self.assertEqual(changed["name"], "新的真实景点")
        self.assertEqual(changed["unit_price"], 90)
        self.assertNotIn("lng", changed)
        self.assertNotIn("lat", changed)
        self.assertEqual(changed["link"], "")
