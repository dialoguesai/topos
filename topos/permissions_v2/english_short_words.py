"""Short English words the Off-limits boundary reads as ordinary words, not names (entity boundary v6).

Generated once from the lower-case entries of Webster's Second International (1934, public domain; the
``web2`` list macOS ships): every word of two or three letters, and every word of three or four letters
ending in "s"; and (v7) every word of four letters with the plurals of the three-letter ones. One entry the
owner-data scanner flags was dropped from each list. ``entity_boundary`` asks two questions
of it: is a protected short alias itself an English word ("ray", "day", "eve"), and is a short form an
English word ("was", "has", "days" are not names). Regenerate only with the same recipe.
"""

WORDS_2_3 = frozenset("""
    aa aal aam aba abb abu aby ace ach act ad add ade ado ady adz ae aer aes aft aga age ago agy ah aha aho ahu
    ai aid ail aim air ait ak aka ake ako aku al ala alb ale alf alk all aln alo alp alt aly am ama ame ami amp
    amt amy an ana and ani ann ant any apa ape apt ar ara arc are ark arm arn art aru arx ary as ase ash ask asp
    ass ast at ate auh auk aum ava ave avo aw awa awd awe awl awn ax axe ay aye ayu azo ba baa bac bad bae bag
    bah bal bam ban bap bar bas bat baw bay be bed bee beg bel ben ber bes bet bey bib bid big bin bis bit biz
    blo bo boa bob bod bog bom bon boo bop bor bot bow boy bra bu bub bud bug bum bun bur bus but buy by bye ca
    cab cad cag cal cam can cap car cat caw cay ce cee cel cep cha che chi cho cig cit cly cob cod coe cog col
    con coo cop cor cos cot cow cox coy coz cro cry cub cud cue cum cup cur cut cwm cyp da dab dad dae dag dah
    dak dal dam dan dao dap dar das daw day de deb dee deg den dev dew dey dha dhu di dib did die dig dim din
    dip dis dit div do dob doc dod doe dog dom don dop dor dos dot dow dry dub dud due dug dum dun duo dup dux
    dye ea ean ear eat ebb edh eel eer eft egg ego eh eke el elb eld elf elk ell elm els elt em eme emu en end
    ens eon er era erd ere erg err ers es ess eta eu eve ewe ex ey eye eyn fa fad fae fag fam fan far fat fay fe
    fed fee fei fen fet feu few fey fez fi fib fid fie fig fin fip fir fit fix flu fly fob fod foe fog foo fop
    for fot fou fow fox foy fra fro fry fu fub fud fug fum fun fur fut ga gab gad gag gaj gal gam gan gap gar
    gas gat gau gaw gay gaz ge ged gee gel gem gen geo ger get gey gez gib gid gie gif gig gim gin gio gip git
    gnu go goa gob god gog goi gol gon goo gor gos got goy gra grr gud gue gul gum gun gup gur gut guy guz gym
    gyn gyp ha had hag hah hak ham han hao hap hat hau haw hay he hei hem hen hep her het hew hex hey hi hia hic
    hid hie him hin hip his hit ho hob hod hoe hog hoi hop hot how hox hoy hub hud hue hug huh hum hup hut hyp
    iao iba ice ich icy id ide ie if ife ihi ilk ill imi imp imu in ing ink inn io ion ire irk is ism iso ist it
    its iva ivy iwa iyo jab jag jam jap jar jaw jay jed jet jib jig jo job joe jog jot jow joy jud jug jut ka
    kai kan kat kay kea keb ked kef keg ken kep ket kex key khu kid kil kim kin kip kit ko koa kob koi kon kop
    kor kos kou kra kyl la lab lac lad lag lai lak lam lan lap lar las lat law lax lay lea led lee leg lei lek
    let leu lev lew ley li lid lie lim lin lip lis lit lo loa lob lod lof log loo lop lot low lox loy lue lug
    lum lut lux ly lye lys ma mac mad mae mag mal man mao map mar mas mat mau maw may me mel mem men met mew mho
    mi mib mid mig mil mim min mir mix mo mob mog mon moo mop mor mot mou mow moy mu mud mug mum mun mux my na
    naa nab nae nag nak nam nan nap nar nat naw nay ne nea neb nee nef nei neo nep net new ni nib nid nig nil
    nim nip nit nix no noa nob nod nog non nor not now noy nth nu nub nul nun nut nye oaf oak oam oar oat obe
    obi och ock od oda odd ode oe oer oes of off oft oh ohm oho oii oil oka oki old olm om on ona one ons ope
    opt or ora orb orc ore orf ort ory os ose ouf our out ova ow owd owe owk owl own ox oxy pa pac pad pah pal
    pam pan pap par pat pau paw pax pay pea ped pee peg pen pep per pes pet pew phi pho phu pi pia pic pie pig
    pik pin pip pir pit pix ply po pob pod poe poh poi pol pom pon pop pot pow pox poy pro pry psi pst pu pua
    pub pud pug pul pun pup pur pus put pya pyr pyx qua quo ra rab rad rag rah raj ram ran rap ras rat raw rax
    ray re rea reb red ree ref reg reh rel rep ret rev rex rhe rho ria rib rid rie rig rim rio rip rit rix rob
    roc rod roe rog roi rot row rox rub rud rue rug rum run rut rux rye sa saa sab sac sad sag sah sai saj sal
    sam san sao sap sar sat saw sax say se sea sec see seg sen ser set sew sex sey sh sha she shi sho shy si sib
    sic sie sig sil sin sip sir sis sit six ski sky sla sly sma sny so sob soc sod soe sog soh sok sol son sop
    sot sou sov sow soy spa spy sri ssu st sty sub sud sue sum sun sup sur suz swa sye ta taa tab tad tae tag
    tai taj tal tam tan tao tap tar tat tau tav taw tax tay tch tck te tea tec ted tee teg ten tew tez th tha
    the tho thy ti tib tic tid tie tig til tin tip tit tji to toa tod toe tog toi tol tom ton too top tor tot
    tou tow tox toy tra tri try tst tu tua tub tue tug tui tum tun tup tur tut tux twa two tye tyg tyt ubi udo
    ug ugh uji uke ula ule ull ulu um ume ump umu un up upo ur ura urd ure urf urn us use ush ust ut uta utu uva
    vag van vas vat vau vee vei vet vex via vie vim vis voe vog vol vow vug vum wa wab wad wae wag wah wan wap
    war was wat waw wax way we web wed wee wem wen wer wet wey wha who why wi wid wig wim win wir wis wit wiz wo
    wob wod woe wog wok won woo wop wot wow woy wro wry wud wun wup wur wut wy wye wyn xi ya yad yah yak yam yan
    yap yar yas yat yaw ye yea yed yee yen yeo yep yer yes yet yew yex yez yin yip yis ym yn yo yoe yoi yok yom
    yon yor yot you yow yox yoy yr yuh yus za zac zad zag zak zar zat zax zed zee zel zer zig zip zo zoa zoo
""".split())

WORDS_ENDING_S_3_4 = frozenset("""
    abas aes alas alms anes anis anus arms ass ates atis axes axis bas bass bats bees bes bias bios bis blas
    boss bus buss ceps cess cos coss crus cuss dags dais das days dess dibs digs dis diss does dogs dos doss
    dubs eats els ens epos eros ers ess exes eyas fass feis fels fess fils fons fuss gas gaus gens gers goes gos
    gris gros guss gyps hals hers his hiss huss ibis ides inks iris its iwis jass jess joss kans kiss kos kras
    las lass lees lens less lis liss lors loss lots lues lys mas mass mess mias mids miss moss muss nabs nais
    naos ness news nibs nobs nous odds oes ons onus oons opus ours pais pass pes pess phos piss plus pobs poss
    pus puss quis rais ras reis reps ross sans sass seps sess sis siss sons soss sots suds taps tars tass taws
    this thus togs tops toss tuts twas upas urus utas vas vis was ways wels wips wis wiss wops wots wuss wyss
    yas yaws yees yes yis yus
""".split())

WORDS_4 = frozenset("""
    aals aams abac abas abbs abed abet abey abir able ably abox abus abut abys acca aces ache achs achy acid
    acle acme acne acor acre acta acts actu acyl adad adat adaw aday adda adds ades adet adit admi ados adry
    adys adze adzs aeon aero aers aery aess afar affa affy afts agal agar agas aged agee agen ager ages agha
    agio agla agog agon agos agre agua ague agys ahas ahem ahey ahos ahoy ahum ahus aide aids aiel aile ails
    aims aint aion aire airs airt airy aits ajar ajog akas akee akes akey akia akin akos akov akra akus alan
    alar alas alba albe albs alco alec alee alef alem alen ales alfa alfs alga alif alin alit alks alky alls
    ally alma alme alms alns alod aloe alop alos alow alps also alto alts alum alys amah amar amas amba ambo
    amen ames amic amid amil amin amir amis amla amli amma ammo ammu amok amor amps amra amts amyl amys anal
    anam anan anas anay anba anda ands anes anew ango anil anis ankh anna anns anoa anon ansa ansu anta ante
    anti ants antu anus anys apar apas aper apes apex apii apio apod apse apts aqua aquo arad arar aras arba
    arca arch arcs ardu area ared ares argo aria arid aril arks arms army arna arni arns arow arse arts arty
    arui arus arxs aryl arys asak asci asem ases ashs ashy asks asok asop asor asps asse assi asss asta asts
    atap atef ates atip atis atle atma atmo atom atop atry atta atwo aube auca auge augh auhs auks aula auld
    aulu aums aune aunt aura ausu aute auto aval avas aver aves avid avos avow awag awas awat away awds awee
    awes awfu awin awls awns awny awry axal axed axes axil axis axle axon ayah ayes ayin ayus azon azos azox
    baal baar baas baba babe babu baby bach back bacs bade bads baes baff baft baga bago bags baho bahs baht
    bail bain bait baka bake baku bald bale bali balk ball balm bals balu bams banc band bane bang bani bank
    bans bant baps bara barb bard bare bari bark barm barn bars baru base bash bask bass bast bate bath bats
    batt batz baud baul baun bawd bawl bawn baws baya bays baze bead beak beal beam bean bear beat beau beck
    beds beef beek been beer bees beet bego begs behn bela beld bell bels belt bely bema bena bend bene beng
    beni benj benn beno bens bent bere berg berm bers besa bess best beta beth bets bevy beys bhat bhoy bhut
    bias bibb bibi bibs bice bick bide bids bien bier biff biga bigg bigs bija bike bikh bile bilk bill bilo
    bind bine bing binh bink bino bins bint biod bion bios bird biri birk birl birn birr biss bite biti bito
    bits bitt biwa bizs bizz blab blad blae blah blan blas blat blaw blay bleb bled blee bleo blet blip blob
    bloc blos blot blow blub blue blup blur boar boas boat boba bobo bobs boce bock bode bods body boga bogo
    bogs bogy boho boid boil bojo boke bola bold bole bolk boll bolo bolt boma bomb boms bond bone bong bonk
    bons bony boob bood boof book bool boom boon boor boos boot bops bora bord bore borg borh born boro bors
    bort bose bosh bosk bosn boss bota bote both bots bott boud bouk boun bout bouw bowk bowl bows boxy boys
    boza bozo brab brad brae brag bran bras brat braw bray bred bree brei bret brew brey brig brim brin brit
    brob brod brog broo brot brow brut bual buba bubo bubs buck buda buds buff bufo bugs buhl buhr bukh bulb
    bulk bull bult bump bums buna bund bung bunk buns bunt buoy burd bure burg buri burl burn buro burp burr
    burs burt bury bush busk buss bust busy buts butt buys buzz byee byes bygo byon byre byth caam caba cabs
    cack cade cadi cads cafh cage cags caid cain cake caky calf calk call calm calp cals calx camb came camp
    cams cand cane cank cans cant cany cape caph caps card care cark carl carp carr cars cart case cash cask
    cast cate cats cauk caul caum caup cava cave cavy cawk caws cays caza cede cees ceil cell cels celt cent
    cepa cepe ceps cere cern cero cess cest ceti chaa chab chad chai chal cham chao chap char chas chat chaw
    chay chee chef ches chew chia chic chid chih chil chin chip chis chit chob chol chop chos chow chub chug
    chum chun chut cigs cine cion cipo cise cist cite cits city cive clad clag clam clan clap clat claw clay
    cled clee clef cleg clem clep clew clip clit clod clog clop clot clow cloy club clue clys coak coal coat
    coax cobs coca cock coco coda code codo cods coed coes coff coft cogs coho coif coil coin coir coke coky
    cola cold cole coli colk coll colp cols colt coly coma comb come cond cone conk conn cons cony coof cook
    cool coom coon coop coos coot copa cope copr cops copy cora cord core corf cork corm corn corp cors cosh
    coss cost cosy cote coth coto cots coue coul coup cove cowl cows cowy coxa coxs coxy coyo coys coze cozs
    cozy crab crag cram cran crap craw crea cree crew crib cric crig crin croc crop cros crow croy crum crus
    crux crys cube cubi cubs cuck cuds cues cuff cuir cuke cull culm cult cump cums cups curb curd cure curl
    curn curr curs curt cush cusk cusp cuss cute cuts cuvy cuya cwms cyan cyke cyma cyme cyps cyst czar dabb
    dabs dace dada dade dado dads daer daes daff daft dags dahs dain dais daks dale dali dalk dals dalt dama
    dame damn damp dams dand dang dank dans daos daps dare darg dari dark darn darr dars dart dash dasi dass
    data date daub daud daut dauw davy dawn daws days daze dazy dead deaf deal dean dear debs debt deck dedo
    deed deem deep deer dees deft defy degs degu dele delf dell deme demi demy dene dens dent deny depa dere
    derm dern desi desk dess deul deva devs dews dewy deys dhai dhak dhan dhas dhaw dhow dhus dial dian dibs
    dice dich dick dids dieb diem dier dies diet digs dika dike dill dilo dime dims dine ding dink dins dint
    diol dips dird dire dirk dirl dirt disc dish disk diss dita dite dits diva dive divs dixy doab doat dobe
    dobs doby dock docs dodd dodo dods doer does doff doge dogs dogy doit doke dola dole doli doll dolt dome
    domn doms domy done dong dons dont doob dook dool doom doon door dopa dope dops dorm dorn dorp dors dory
    dosa dose doss dote dots doty douc doum doup dour dout dove dowd dowf dowl down dowp dows doxa doxy doze
    dozy drab drag dram drat draw dray dree dreg drew drib drip drop drow drub drug drum drys duad dual dubb
    dubs duck duct dude duds duel duer dues duet duff dugs duim duit duke dull dult duly duma dumb dump dums
    dune dung dunk duns dunt duny duos dupe dups dura dure durn duro dush dusk dust duty duxs dyad dyce dyer
    dyes dyke dyne each eans earl earn ears ease east easy eats eave ebbs eboe ebon ecad eche echo ecru eddo
    eddy edea edge edgy edhs edit eels eely eers efts egad eggs eggy egma egol egos eheu ejoo eker ekes ekka
    elbs elds elfs elks elle ells elms elmy elod else elss elts emes emir emit emma empt emus emyd enam ends
    enol enow ense enss envy eoan eons epee epha epic epos eral eras erds eres ergs eria eric erne eros errs
    erss erth eruc esca esne espy esss etas etch etna etua etui etym euge even ever eves evil evoe ewer ewes
    ewry exam exes exit exon eyah eyas eyed eyen eyer eyes eyey eyne eyns eyot eyra eyre ezba face fack fact
    facy fade fads fady faes faff fage fags fail fain fair fake faky fall falx fame fams fana fand fang fans
    fant faon fare farl farm faro fars fash fass fast fate fats faun favn fawn fays faze feak feal fear feat
    feck feds feed feel feer fees feif feil feis fell fels felt feme fend fens fent feod ferk fern feru fess
    fest fets feud feus fews feys fezs fiar fiat fibs fice fico fide fids fies fife fifo figs fike file fill
    film filo fils find fine fink fins fips fire firk firm firn firs fisc fise fish fist fits five fixs fizz
    flag flak flam flan flap flat flaw flax flay flea fled flee flet flew flex fley flip flit flix flob floc
    floe flog flop flot flow flub flue flus flux flys foal foam fobs foci fods foes fogo fogs fogy foil fold
    fole folk fond fono fons font food fool foos foot fops fora forb ford fore fork form fors fort fosh fots
    foud foul foun four fous fowk fowl fows foxs foxy foys fozy frab frae frap fras frat fray free fret frib
    frig frim frit friz froe frog from fros frot frow frys fubs fuci fuds fuel fuff fugs fugu fuji fulk full
    fume fums fumy fund funk funs funt furl furs fury fusc fuse fuss fust fute futs fuye fuze fuzz fyke fyrd
    gabi gabs gaby gade gads gaen gaet gaff gage gags gain gair gait gajs gala gale gali gall galp gals galt
    gamb game gamp gams gamy gane gang gans gant gaol gapa gape gapo gaps gapy gara garb gare garn gars gash
    gasp gass gast gata gate gats gaub gaud gaum gaun gaup gaur gaus gaut gave gawk gawm gawn gaws gays gaze
    gazi gazs gazy geal gean gear geat geck geds geek gees geet gegg gein geld gell gels gelt gems gena gene
    gens gent genu geos gerb germ gers gest geta gets geum geys gezs ghat ghee gibe gibs gids gied gien gies
    gifs gift gigs gild gill gilo gilt gimp gims ging gink gins gios gips gird girl girn giro girr girt gish
    gist gith gits give gizz glad glam glar glee gleg glen glia glib glom glop glor glow gloy glub glue glug
    glum glut gnar gnat gnaw gnus goad goaf goal goas goat gobi gobo gobs goby gode gods goel goer goes goff
    gogo gogs gois gola gold golf goli gols gone gong gons gony good goof gook gool goon goos gora gorb gore
    gors gory gosh goss gote gots goup gout gove gowf gowk gowl gown goys grab grad gram gras grat gray gree
    grew grey grid grig grim grin grip gris grit grog gros grot grow grrs grub grue grum grun guan guao guar
    gude guds gues gufa guff gugu guhr guib gula gule gulf gull gulp guls gump gums guna gunj gunk gunl guns
    gups gurk gurl gurr gurs gurt guru gush guss gust guts gutt guys guze guzs gwag gyle gyms gyne gyns gype
    gyps gyre gyri gyro gyte gyve haab haaf habu hack hade hadj hads haec haem haet haff haft hagi hags hahs
    haik hail hain hair haje hake hako haks haku hala hale half hall halo hals halt hame hami hams hand hank
    hans hant haos haps hapu hard hare hark harl harm harn harp harr hart hash hask hasp hate hath hats hatt
    haul haus have hawk hawm haws haya hays hayz haze hazy head heaf heal heap hear heat hech heck heed heel
    heer heft heii heir heis hele hell helm help heme heml hemp hems hend hens hent heps herb herd here herl
    hern hero hers hest hets hevi hewn hews hewt hexa hexs heys hias hick hics hide hids hies high hike hill
    hilt himp hims hind hing hins hint hipe hips hire hiro hish hisn hiss hist hits hive hizz hoar hoax hobo
    hobs hock hods hoer hoes hoga hogs hoin hois hoit hoju hold hole holl holm holt holy home homo homy hone
    hong honk hood hoof hook hoon hoop hoot hope hopi hops hora horn hory hose host hoti hots hour hove howe
    howk howl hows hoxs hoys hubb hubs huck huds hued huer hues huff huge hugs huhs huia huke hula hulk hull
    hulu hump hums hung hunh hunk hunt hups hura hure hurl hurr hurt huse hush husk huso huss huts huzz hyke
    hyle hymn hyne hypo hyps iamb iaos ibas ibex ibid ibis iced ices icho ichs ichu icon icys idea ides idic
    idle idly idol idyl ifes iffy ihis iiwi ijma ikat ikey ikra ilex ilia ilka ilks ills illy ilot imam imbe
    imis immi impi imps impy imus inbe inby inch inde indy ings inks inky inly inns inro into iodo ions iota
    ipid ipil ires irid iris irks irok iron isba isle isms ismy isos ists itch item iter itmo itss ivas ivin
    ivys iwas iwis iyos izar izle jabs jack jacu jade jady jags jail jake jako jama jamb jami jams jane jank
    jann jaob jape japs jara jarg jarl jars jass jati jato jauk jaun jaup jaws jawy jays jazz jean jeds jeel
    jeep jeer jeff jehu jell jerk jerl jerm jert jess jest jete jets jhow jibe jibi jibs jiff jigs jilt jimp
    jina jing jink jinn jinx jiti jiva jive jobo jobs joch jock jocu joes joey jogs join joke joky joll jolt
    joom josh joss jota jots joug jouk jowl jows joys juba jube juck judo juds jugs juju juke jump june junk
    junt jupe jure jury just jute juts jynx kago kagu kaha kahu kaid kaik kail kais kaka kaki kala kale kali
    kalo kame kana kang kans kapa kapp karo kasa kasm kath kats kava kayo kays kazi keas kebs keck keds keek
    keel keen keep keet kefs kegs keld kele kelk kell kelp kelt kemb kemp kend keno kens kent kepi keps kept
    kerf kern keta keto kets keup kexs kexy keys khan khar khat khet khir khot khus kibe kiby kick kids kiel
    kier kike kiki kiku kill kiln kilo kilp kils kilt kims kina kind king kink kino kins kipe kips kiri kirk
    kirn kish kiss kist kite kith kits kiva kivu kiwi kiyi klam klip klom klop kmet knab knag knap knar knee
    knet knew knez knit knob knop knot know knub knur knut koae koas kobi kobs kobu koda koel koff koft kohl
    koil kois koko koku kola kolo kona kons koph kopi kops kora kore kori kors koss koto kous kozo kral kran
    kras kuan kuba kudu kuei kuge kuku kula kulm kung kunk kusa kwan kyah kyar kyat kyle kyls kyte labs lace
    lack lacs lacy lade lads lady laet lags laic laid lain lair lais lake laks laky lall lalo lama lamb lame
    lamp lams land lane lank lans lant lanx laps lard lari lark lars lasa lash lask lass last lata late lath
    lats laud laun laur lava lave lawk lawn laws laxs lays laze lazy lead leaf leak leal leam lean leap lear
    leas leat lech leck lede leds leed leek leep leer lees leet left legs lehr leis leks lend lene leno lens
    lent lepa lerp less lest lete lets leud leuk leus levo levs levy lewd lews leys liar lice lich lick lido
    lids lied lief lien lier lies lieu life lifo lift liin lija like lile lill lilt lily limb lime limn limo
    limp lims limu limy lina line ling link linn lino lins lint liny lion lipa lips lira lire lish lisk lisp
    liss list lite lith lits litz live llyn load loaf loam loan loas lobe lobo lobs loca loch loci lock loco
    lode lods lofs loft loge logo logs logy loin loir loka loke loll loma lone long lood loof look loom loon
    loop loos loot lope lops lora lord lore lori lorn loro lors lory lose losh loss lost lota lote lots loud
    louk loup lour lout love lowa lown lows lowy loxs loys lube luce luck lucy ludo lues luff luge lugs luke
    lull lulu lump lums luna lune lung lunn lunt lupe lura lure lurg lurk lush lusk lust lute luts luxe luxs
    lyam lyes lynx lyra lyre lyse lyss maam mabi mace mack maco macs made mado mads maes mage magi mags maha
    maid mail maim main majo make maki mako mala male mali mall malm malo mals malt mamo mana mand mane mang
    mani mank mano mans mant many maos mapo maps marc mare mark marl marm maro mars mart maru mary masa mash
    mask mass mast masu mate math mats maty maud maul maun maus maux mawk mawp maws maya mays maza maze mazy
    mead meak meal mean meat meed meek meet mein meio mela meld mele mell mels melt memo mems mend meng mens
    menu meny mere merk merl mero mesa mese mesh meso mess meta mete mets mewl mews mhos mian mias mibs mica
    mice mick mico mide mids mien miff migs mijl mike mila mild mile milk mill milo mils milt mima mime mimp
    mims mina mind mine ming mink mino mins mint minx miny mird mire mirk miro mirs miry mise miss mist mite
    mitt mity mixs mixy moan moat mobs mock mode moff mogo mogs moha moho mohr moil moio moit mojo moke moki
    moko moky mola mold mole moll molt moly mome momo mona mone mong monk mono mons mood mool moon moop moor
    moos moot mope moph mops mora more morg morn moro mors mort moss most mote moth mots mott moud moul moup
    mous mout move mown mows mowt moxa moyo moys much muck mudd muds muff muga mugg mugs muid muir mule mulk
    mull mult mump mums mund mung munj muns munt mura mure murk muse mush musk muss must muta mute muth mutt
    muxs muzz myal myna myst myth myxa myxo naam naas nabk nabs nace nach nael naes naga nags naid naif naig
    naik nail nain naio nais nake nako naks name nams nana nane nans nant naos napa nape naps napu nard nark
    narr nars nary nash nasi nast natr nats naut nave navy naws nawt nays naze neal neap near neas neat nebs
    neck need neem neep neer nees neet nefs neif neis nema neon neos neps nese nesh ness nest nete neth neti
    nets neve nevo news newt next ngai nibs nice nick nide nidi nids nife nigh nigs nils nimb nims nine niog
    nipa nips nito nits nixs nizy noas nobs nock node nodi nods noel nogs noil noir noll nolo noma nome none
    nons nook noon noop nope nori norm nors nose nosh nosy note nots noun noup nous nova nows nowt nowy noxa
    noys nths nubs nude nuke null nuls numb nuns nuts nyes oafs oaks oaky oams oars oary oast oath oats oaty
    oban obes obex obey obis obit oboe obol ochs ocht ocks odal odas odds odel odes odic odor odso odum odyl
    oers oess offs ofts ogam ogee ogle ogre ogum ohia ohms ohos ohoy oiis oils oily oime oint okas okee oket
    okia okis okra olam olds oleo olid olio olla olms olpe omao omen omer omit onas onca once ondy oner ones
    only onss onto onus onym onyx onza oofy ooid oons oont oord ooze oozy opah opal open opes opsy opts opus
    orad oral oras orbs orby orcs ordu ores orfs orgy orle orlo orna orts orys oses osse otic otto ouch oufs
    ough ours oust outs oval ovas oven over ovey ovum owds ower owes owks owls owly owns owse oxan oxea oxen
    oxer oxyl oxys oyer paal paar paca pace pack paco pacs pact pads paga page paha pahi paho pahs paik pail
    pain paip pair pais pale pali pall palm palp pals palt paly pams pand pane pang pank pans pant paon papa
    pape paps para pard pare pari park parr pars part pash pasi pass past pata pate path pato pats patu paty
    paup paus paut pave pavy pawk pawl pawn paws paxs pays peag peai peak peal pean pear peas peat peba pech
    peck peda peds peed peek peel peen peep peer pees pega pegs peho pelf pell pelt pelu pend penk pens pent
    peon pepo peps peri perk perm pern pers pert pesa peso pess pest pete peto pets pews pewy pfui phew phis
    phit phiz phoh phon phoo phos phot phus phut pial pian pias pica pice pick pico pics pict pied pien pier
    pies piet piff pigs pika pike piki piks piky pile pili pill pilm pily pimp pina pind pine ping pink pino
    pins pint piny pipa pipe pipi pips pipy pirl pirn pirr pirs pise pish pisk piso piss pist pita pith pits
    pity pixs pixy pize plak plan plap plat play plea pleb pled plew plex plim plod plop plot plow ploy plud
    plug plum plup plus plys pobs pock poco pods poem poes poet pogy poha pohs poil pois poke poky pole polk
    poll polo pols polt poly pome pomp poms pond pone pong pons pont pony pooa poof pooh pook pool poon poop
    poor poot pope pops pore pork porr port pory pose posh poss post posy pote pots pott pouf pour pout pows
    poxs poxy poys prad pram prat prau pray prep prey prig prim proa prob prod prof prog proo prop pros prow
    pruh prut prys psha psis psts puan puas pubs puce puck puds pudu puff pugh pugs puja puka puke puku puky
    pule puli pulk pull pulp puls pulu puly puma pump puna pung punk puns punt puny pupa pups pure purl purr
    purs push puss puts putt puxy pyal pyas pyic pyin pyke pyla pyre pyro pyrs pyxs qere qeri qoph quab quad
    quag quan quar quas quat quaw quay quei quet quey quib quid quin quip quis quit quiz quod quop quos quot
    raad rabs race rach rack racy rada rads raff raft raga rage rags rahs raia raid rail rain rais raja rajs
    rake rakh raki raku rale rame rami ramp rams rana rand rane rang rani rank rann rans rant rape raps rapt
    rare rasa rase rash rasp rass rata rate rath rats rauk raun rave raws raxs raya rays raze razz read reak
    real ream reap rear reas rebs reck rect redd rede redo reds reed reef reek reel reem reen rees reet refs
    reft regs rehs reif reim rein reis reit rels rely rend renk rent repp reps resh resp rest rets reve revs
    rexs rhea rhes rhos rial rias ribe ribs rice rich rick ride rids riem rier ries rife riff rift rigs rikk
    rile rill rima rime rims rimu rimy rind rine ring rink rios riot ripa ripe rips rise risk risp rist rita
    rite rits riva rive rixs rixy road roam roan roar robe robs rock rocs rodd rode rods roed roer roes roey
    rogs roid roil rois roit roka roke roky role roll romp rond rone rood roof rook rool room roon root rope
    ropp ropy rory rose ross rosy rota rote roto rots roub roud roue roun roup rout rove rows rowy roxs roxy
    royt rubs ruby ruck rudd rude ruds ruen ruer rues ruff ruga rugs ruin rukh rule rull rump rums rune rung
    runs runt rupa ruru ruse rush rusk rust ruth ruts ruxs ryal ryen ryes ryme rynd rynt ryot rype saas sabe
    sabs sack saco sacs sade sadh sado sadr sads safe saft saga sage sago sags sagy sahh sahs saic said sail
    saim sain saip sair sais sajs sake saki sale salp sals salt same samh samp sams sand sane sang sank sans
    sant saos sapa sapo saps sard sare sari sark sars sart sasa sash sass sate sats sauf saum saur saut save
    sawn saws sawt saxs saya says scab scad scam scan scap scar scat scaw scho scob scog scot scow scry scud
    scug scum scun scup scur scut scye scyt seah seak seal seam sear seas seat seax sech seck secs sect seed
    seek seel seem seen seep seer sees sego segs seit sele self sell selt seme semi send sens sent seps sept
    sera sere serf sero sers sert sess seta seth sets sett sewn sews sexs sext sexy seys shab shad shag shah
    sham shan shap shas shat shaw shay shea shed shee sher shes shih shim shin ship shis shiv shod shoe shog
    shoo shop shoq shor shos shot shou show shug shul shun shut shys siak sial sibs sice sick sics side sidi
    sidy sier sies sife sift sigh sign sigs sika sike sile silk sill silo sils silt sima sime simp sina sind
    sine sing sinh sink sins siol sion sipe sips sire sirs sise sish sisi siss sist site sith sits siva sixs
    size sizy sizz skag skal skat skaw skee skeg skel sken skeo skep sker skew skey skid skil skim skin skip
    skis skit skiv skoo skua skun skys slab slad slae slag slam slap slas slat slaw slay sled slee slew sley
    slid slim slip slit slob slod sloe slog slon sloo slop slot slow slub slud slue slug slum slur slut slys
    smas smee smew smit smog smug smur smut snab snag snap snaw sneb sned snee snew snib snig snip snob snod
    snog snop snot snow snub snug snum snup snur snys soak soam soap soar sobs soce sock soco socs soda sods
    sody soes sofa soft sogs soho sohs soil soja soka soke soks sola sold sole soli solo sols soma some sond
    song sonk sons sook sool soon soot sope soph sops sora sorb sore sori sorn sort sory sosh soso soss sots
    soud soul soum soup sour sous sovs sowl sown sows sowt soya soys spad spae spak spam span spar spas spat
    spay spec sped spet spew spex spig spin spit spiv spor spot spry spud spug spun spur sput spys sris ssus
    stab stag stam stap star staw stay steg stem sten step stet stew stey stib stid stim stir stoa stob stod
    stof stog stop stot stow stra stre stub stud stue stug stum stun stut stys subs such suck sudd suds suer
    sues suet suff sugh sugi suid suit suji suld sulk sull sump sums sune sung sunk sunn suns sunt supa supe
    sups sura surd sure surf surs susi susu suum suwe suzs swab swad swag swam swan swap swas swat sway swep
    swig swim swiz swob swom swot swow swum syce syes sync syne syre syrt taar taas tabs tabu tach tack tact
    tade tads tael taen taes taft tags taha tahr tail tain tais tait tajs take takt taky tala talc tald tale
    tali talk tall tals tame tamp tams tana tane tang tanh tank tans taos tapa tape taps tapu tara tare tari
    tarn taro tarp tarr tars tart tash task tass tasu tate tath tats tatu taum taun taur taus taut tave tavs
    tawa tawn taws taxi taxs taxy tays tche tchs tchu tcks tead teak teal team tean teap tear teas teat teca
    tech teck tecs teds teel teem teen teer tees teet teff tegs teil teju tele teli tell telt temp tend teng
    tens tent tera term tern terp test tete teth teuk tews text tezs than thar thas that thaw theb thee them
    then thes thew they thig thin thio thir this thob thof thon thoo thos thou thow thro thud thug thus thys
    tiao tiar tibs tice tick tics tide tids tidy tied tien tier ties tiff tift tige tigs tile till tils tilt
    time tind tine ting tink tins tint tiny tipe tips tire tirl tirr tite titi tits tivy tiza tjis toad toas
    toat tobe toby tock toco tode tods tody toed toes toff toft tofu toga togs togt toho toil tois toit toke
    toko told tole toll tols tolt tolu tomb tome toms tone tong tonk tons tony took tool toom toon toop toos
    toot tope toph topi topo tops tora torc tore torn toro tors tort toru tory tosh toss tost tosy tote toto
    tots toty toug toup tour tous tout towd town tows towy toxa toxs toys toze trag trah tram trap tras tray
    tree tref trek tret trey trig trim trin trio trip tris trod trog tron trot trow troy trub true trug trun
    tryp trys tryt tsar tsia tsts tsun tuan tuas tuba tube tubs tuck tues tufa tuff tuft tugs tuik tuis tuke
    tula tule tume tump tums tuna tund tune tung tunk tuno tuns tunu tuny tups turb turd turf turk turm turn
    turp turr turs tush tusk tute tuth tuts tutu tuwi tuxs tuza twae twal twas twat tway twee twig twin twit
    twos tyee tyes tygs tyke tymp tynd type typo typp typy tyre tyro tyts uang ubis udal udos ughs ugly uily
    ujis ukes ulas ules ulex ulla ulls ulmo ulna ulua ulus umbo umes umph umps umus unal unau unbe unca unci
    unco unde undo undy unie unio unit unto untz unze upas updo upgo upla upon upos ural uran urao uras urde
    urds urea ures urfs urge uric urna urns urus urva usar used usee user uses ushs usts utai utas utch utum
    utus uval uvas uvea uvic uvid uzan vade vady vage vags vail vain vair vale vali vall vamp vane vang vans
    vara vare vari vary vasa vase vass vast vasu vats vaus veal veen veep veer vees veil vein veis vela vell
    velo vend vent vera verb verd veri vert very vest veta veto vets vexs vext vial vias vice vier vies view
    viga vila vile vill vims vina vine vino vint viny viol vire virl visa vise viss vita viva vive vlei voar
    voes voet vogs void vole vols volt vota vote vows vugs vuln vums waag waar wabe wabs wace wack wade wadi
    wads waeg waer waes waff waft wage wags wahs waif waik wail wain wait waka wake wakf waky wale wali walk
    wall walt wame wamp wand wane wang wans want wany wapp waps ward ware warf wark warl warm warn warp wars
    wart wary wase wash wasp wass wast wath wats watt wauf waul waup waur wave wavy wawa waws waxs waxy ways
    weak weal weam wean wear webs wede weds weed week weel ween weep wees weet weft weir weka weki weld welk
    well wels welt wems wend wene wens went wept were werf weri wers wert wese west weta wets weve weys wham
    whan whap whar whas what whau whee when whet whew whey whid whig whim whin whip whir whit whiz whoa whom
    whoo whop whos whud whun whup whuz whyo whys wice wick wide wids widu wife wigs wild wile wilk will wilt
    wily wime wimp wims wind wine wing wink wins wint winy wipe wips wird wire wirl wirr wirs wiry wise wish
    wisp wiss wist wite with wits wive wizs woad woak woan wobs wode wods woes woft wogs woke woks wold wolf
    womb wone wong wons wont wood woof wool woom woon woos wops word wore work worm worn wort wote wots wouf
    wove wows wowt woys wran wrap wraw wren wrig writ wros wrox wrys wuds wudu wugg wulk wull wuns wups wurs
    wush wusp wuss wust wuts wuzu wyde wyes wyke wyle wynd wyne wynn wyns wype wyss wyve xyla xyst yaba yabu
    yade yads yaff yagi yahs yair yaje yaks yalb yale yali yamp yams yang yank yans yapa yapp yaps yarb yard
    yare yark yarl yarm yarn yarr yars yass yate yati yats yaud yava yawl yawn yawp yaws yawy yaya ycie yday
    yeah yean year yeas yeat yede yeds yeel yees yegg yeld yelk yell yelm yelp yelt yeni yens yeos yeps yerb
    yerd yere yerk yern yers yese yeso yess yest yeta yeth yets yeuk yews yexs yezs yigh yill yilt yins yips
    yird yirk yirm yirn yirr yiss yite yobi yock yodh yoes yoga yogh yogi yois yoke yoks yoky yolk yoms yond
    yons yont yook yoop yore york yors yote yots youd youl youp your yous yowl yows yowt yoxs yoys yuan yuca
    yuck yuft yuhs yule yurt yuss yutu zacs zads zags zain zaks zant zany zarf zarp zars zati zats zaxs zeal
    zebu zeds zeed zees zein zels zemi zenu zero zers zest zeta zigs zimb zinc zing zink zips zira zizz zoas
    zobo zoea zogo zoic zoid zoll zone zoom zoon zoos zuza zyga zyme
""".split())
