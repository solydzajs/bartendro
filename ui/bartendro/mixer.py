# -*- coding: utf-8 -*-
from time import sleep, time
from threading import Thread
from flask import Flask, current_app
from flask.ext.sqlalchemy import SQLAlchemy
import memcache
from sqlalchemy.orm import mapper, relationship, backref
from bartendro import db, app
from bartendro.model.drink import Drink
from bartendro.model.dispenser import Dispenser
from bartendro.model import drink_booze
from bartendro.model import booze
from bartendro.model import drink_log

TICKS_PER_ML = 2.78
CALIBRATE_ML = 60 
CALIBRATION_TICKS = TICKS_PER_ML * CALIBRATE_ML

LIQUID_OUT_THRESHOLD       = 75
LIQUID_WARNING_THRESHOLD   = 120 

DISPENSER_OUT     = 1
DISPENSER_OK      = 0
DISPENSER_WARNING = 2

class Mixer(object):
    '''This is where the magic happens!'''

    class MixerState:
        INIT = object()            # the bot is initializing
        READY = object()           # the bot is ready to make drinks
        WARNING = object()         # one or more bottles of booze is low
        OUT_OF_BOOZE = object()    # one of more bottles of booze is OUT
        # not in use yet
        # BUSTED = object()          # out of all booze, can't make ANY drinks

    def __init__(self, driver, mc, liquid_out):
        self.driver = driver
        self.mc = mc
        self.err = ""
        self.disp_count = self.driver.count()
        self.state = Mixer.MixerState.INIT
        self.use_liquid_out = liquid_out
        self.check_liquid_levels()

    def get_error(self):
        return self.err

    def led_idle(self):
        self.driver.led_idle()

    def led_dispense(self):
        self.driver.led_dispense()

    def led_complete(self):
        self.driver.led_complete()

    def can_make_drink(self, boozes, booze_dict):
        ok = True
        for booze in boozes:
            try:
                foo = booze_dict[booze]
            except KeyError:
                ok = False
        return ok

    def check_liquid_levels(self):
        if not self.use_liquid_out: 
            self.driver.set_status_color(0, 0, 1)
            state = Mixer.MixerState.READY
            return

        new_state = Mixer.MixerState.READY

        # step 1: ask the dispensers to update their liquid levels
        self.driver.update_liquid_levels()

        # wait for the dispensers to determine the levels
        sleep(.01)

        # Now ask each dispenser for the actual level
        dispensers = db.session.query(Dispenser).order_by(Dispenser.id).all()
        for i, dispenser in enumerate(dispensers):
            if i >= self.disp_count: break

            dispenser.out = DISPENSER_OK
            level = self.driver.get_liquid_level(i)
            if level <= LIQUID_WARNING_THRESHOLD:
                if new_state == Mixer.MixerState.READY:
                    new_state = Mixer.MixerState.WARNING
                if dispenser.out != DISPENSER_WARNING:
                    dispenser.out = DISPENSER_WARNING

            if level <= LIQUID_OUT_THRESHOLD:
                if new_state == Mixer.MixerState.READY or new_state == Mixer.MixerState.WARNING:
                    new_state = Mixer.MixerState.OUT_OF_BOOZE
                if dispenser.out != DISPENSER_OUT:
                    dispenser.out = DISPENSER_OUT

        db.session.commit()

        if new_state == Mixer.MixerState.OUT_OF_BOOZE:
            self.driver.set_status_color(1, 0, 0)
        elif new_state == Mixer.MixerState.WARNING:
            self.driver.set_status_color(1, 1, 0)
        else:
            self.driver.set_status_color(0, 0, 1)

        self.state = new_state
        print "Checking levels done"

        return new_state

    def liquid_level_test(self, dispenser, threshold):
        if not self.use_liquid_out: return

        print "Start liquid level test: (disp %s thres: %d)" % (dispenser, threshold)

        self.driver.update_liquid_levels()
        sleep(.01)

        level = self.driver.get_liquid_level(dispenser)
	print "initial reading: %d" % level
        if level <= threshold:
	    print "liquid is out before starting: %d" % level
	    return

        last = -1
        self.driver.start(dispenser)
        while level > threshold:
            self.driver.update_liquid_levels()
            sleep(.01)
            level = self.driver.get_liquid_level(dispenser)
            if level != last:
                 print "  %d" % level
            last = level

        self.driver.stop(dispenser)
        print "Stopped at level: %d" % level
        sleep(.1);
        level = self.driver.get_liquid_level(dispenser)
        print "motor stopped at level: %d" % level

    def get_available_drink_list(self):
        can_make = self.mc.get("available_drink_list")
        if can_make: 
            return can_make

        add_boozes = db.session.query("abstract_booze_id") \
                            .from_statement("""SELECT bg.abstract_booze_id 
                                                 FROM booze_group bg 
                                                WHERE id 
                                                   IN (SELECT distinct(bgb.booze_group_id) 
                                                         FROM booze_group_booze bgb, dispenser 
                                                        WHERE bgb.booze_id = dispenser.booze_id)""")

        if self.use_liquid_out: 
            sql = "SELECT booze_id FROM dispenser WHERE out == 0 ORDER BY id LIMIT :d"
        else:
            sql = "SELECT booze_id FROM dispenser ORDER BY id LIMIT :d"

        boozes = db.session.query("booze_id") \
                        .from_statement(sql) \
                        .params(d=self.disp_count).all()
        boozes.extend(add_boozes)

        booze_dict = {}
        for booze_id in boozes:
            booze_dict[booze_id[0]] = 1

        drinks = db.session.query("drink_id", "booze_id") \
                        .from_statement("SELECT d.id AS drink_id, db.booze_id AS booze_id FROM drink d, drink_booze db WHERE db.drink_id = d.id ORDER BY d.id, db.booze_id") \
                        .all()
        last_drink = -1
        boozes = []
        can_make = []
        for drink_id, booze_id in drinks:
            if last_drink < 0: last_drink = drink_id
            if drink_id != last_drink:
                if self.can_make_drink(boozes, booze_dict): 
                    can_make.append(last_drink)
                boozes = []
            boozes.append(booze_id)
            last_drink = drink_id

        if self.can_make_drink(boozes, booze_dict): 
            can_make.append(last_drink)

        self.mc.set("available_drink_list", can_make)
        return can_make

    def make_drink(self, id, recipe_arg):

        drink = Drink.query.filter_by(id=int(id)).first()
        dispensers = Dispenser.query.order_by(Dispenser.id).all()

        recipe = []
        size = 0
        for booze in recipe_arg:
            r = None
            booze_id = int(booze[5:])
            for i in xrange(self.disp_count):
                disp = dispensers[i]
                if booze_id == disp.booze_id:
                    r = {}
                    r['dispenser'] = disp.id
                    r['dispenser_actual'] = disp.actual
                    r['booze'] = booze_id
                    r['ml'] = recipe_arg[booze]
                    size += r['ml']
                    break
            if not r:
                self.err = "Cannot make drink. I don't have the required booze: %d" % booze_id
                error(self.err)
                return False
            recipe.append(r)
        
        app.log.info("Making drink: '%s' size %.2f ml" % (drink.name.name, size))
        self.led_dispense()
        dur = 0
        active_disp = []
        for r in recipe:
            if r['dispenser_actual'] == 0:
                r['ms'] = int(r['ml'] * TICKS_PER_ML)
            else:
                r['ms'] = int(r['ml'] * TICKS_PER_ML * (CALIBRATE_ML / float(r['dispenser_actual'])))
            self.driver.dispense_ticks(r['dispenser'] - 1, int(r['ms']))
            app.log.info("..dispense %d for %d ticks" % (r['dispenser'] - 1, int(r['ms'])))
            active_disp.append(r['dispenser'])
            sleep(.01)

            if r['ms'] > dur: dur = r['ms']

        while True:
            sleep(.1)
            done = True
            for disp in active_disp:
                if self.driver.is_dispensing(disp - 1): 
                    done = False
                    break
            if done: break

        self.led_complete()
        app.log.info("drink complete")

        t = int(time())
        dlog = drink_log.DrinkLog(drink.id, t, size)
        db.session.add(dlog)
        db.session.commit()

        if self.use_liquid_out and not self.check_liquid_levels():
            self.leds_panic()
            return False

        FlashGreenLeds(self).start()
        return True 

class FlashGreenLeds(Thread):
    def __init__(self, mixer):
        Thread.__init__(self)
        self.mixer = mixer

    def run(self):
        sleep(5);
        self.mixer.led_idle()
